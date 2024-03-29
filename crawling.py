"""A simple web crawler -- class implementing crawling logic."""

import asyncio
import cgi
import logging
import re
import time
import urllib.parse
from collections import namedtuple

import aiohttp
from proxypool import *

LOGGER = logging.getLogger(__name__)


def lenient_host(host):
    parts = host.split('.')[-2:]
    return '.'.join(parts)  # changed from ''


FetchStatistic = namedtuple('FetchStatistic', ['url',
                                               'next_url',
                                               'status',
                                               'exception',
                                               'size',
                                               'content_type',
                                               'encoding',
                                               'num_urls',
                                               'num_new_urls'])


class Crawler:
    """Crawl a set of URLs.

    This manages two sets of URLs: 'urls' and 'done'.  'urls' is a set of
    URLs seen, and 'done' is a list of FetchStatistics.
    """

    def __init__(self, roots, exclude=None, strict=True,  # What to crawl.
                 max_redirect=1, max_tries=3,  # Per-url limits.
                 max_tasks=10, proxy=False, *, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.roots = roots
        self.exclude = exclude
        self.strict = strict
        self.max_redirect = max_redirect
        self.max_tries = max_tries
        self.max_tasks = max_tasks
        if proxy:
            self.proxy = get_proxy()
        else:
            self.proxy = None
        self.q = asyncio.Queue(loop=self.loop)
        self.seen_urls = set()
        self.done = []
        self.session = None
        self.root_domains = set()
        for root in roots:
            parts = urllib.parse.urlparse(root)
            host = urllib.parse.splitport(parts.netloc)[0]
            if not host:
                continue
            if re.match(r'^[\d.]*$', host):
                self.root_domains.add(host)
            else:
                host = host.lower()
                if self.strict:
                    self.root_domains.add(host)
                else:
                    self.root_domains.add(lenient_host(host))
        for root in roots:
            self.add_url(root)
        self.t0 = None
        self.t1 = None

    def host_okay(self, host):
        """Check if a host should be crawled.

        A literal match (after lowercasing) is always good.  For hosts
        that don't look like IP addresses, some approximate matches
        are okay depending on the strict flag.
        """
        host = host.lower()
        if host in self.root_domains:
            return True
        if re.match(r'\A[\d.]*\Z', host):
            return False
        if self.strict:
            return self._host_okay_strictish(host)
        else:
            return self._host_okay_lenient(host)

    def _host_okay_strictish(self, host):
        """Check if a host should be crawled, strict-ish version.

        This checks for equality modulo an initial 'www.' component.
        """
        host = host[4:] if host.startswith('www.') else 'www.' + host
        return host in self.root_domains

    def _host_okay_lenient(self, host):
        """Check if a host should be crawled, lenient version.

        This compares the last two components of the host.
        """
        return lenient_host(host) in self.root_domains

    def record_statistic(self, fetch_statistic):
        """Record the FetchStatistic for completed / failed URL."""
        self.done.append(fetch_statistic)

    async def parse_links(self, response):
        """Return a FetchStatistic and list of links."""
        links = set()
        content_type = None
        encoding = None
        body = await response.read()

        if response.status == 200:
            content_type = response.content_type
            pdict = {}

            if content_type:
                content_type, pdict = cgi.parse_header(content_type)

            encoding = pdict.get('charset', 'utf-8')
            if content_type in ('text/html', 'application/xml'):
                text = await response.text(encoding, "ignore")

                # Replace href with (?:href|src) to follow image links.
                urls = set(re.findall(r'''(?i)href=["']([^\s"'<>]+)''', text))

                for url in urls:
                    normalized = urllib.parse.urljoin(str(response.url), url)
                    defragmented = urllib.parse.urldefrag(normalized)[0]
                    if self.url_allowed(defragmented):
                        links.add(defragmented)

            if links:
                LOGGER.info('got %r distinct urls from %r',
                            len(links), response.url)

        stat = FetchStatistic(
            url=response.url,
            next_url=None,
            status=response.status,
            exception=None,
            size=len(body),
            content_type=content_type,
            encoding=encoding,
            num_urls=len(links),
            num_new_urls=len(links - self.seen_urls))

        return stat, links

    async def fetch(self, url, max_redirect):
        """Fetch one URL."""
        tries = 0
        exception = None
        while tries < self.max_tries:
            try:
                response = await self.session.get(url, allow_redirects=False, proxy=self.proxy)
                if tries > 1:
                    LOGGER.info('try %r for %r success', tries, url)
                    delete_proxy(self.proxy)
                    self.proxy = get_proxy()
                break
            except aiohttp.ClientError as client_error:
                LOGGER.info('try %r for %r raised %r', tries, url, client_error)
                exception = client_error

            tries += 1
        else:
            # We never broke out of the loop: all tries failed.
            LOGGER.error('%r failed after %r tries', url, self.max_tries)
            self.record_statistic(FetchStatistic(url=url,
                                                 next_url=None,
                                                 status=None,
                                                 exception=exception,
                                                 size=0,
                                                 content_type=None,
                                                 encoding=None,
                                                 num_urls=0,
                                                 num_new_urls=0))
            return

        try:
            if response.status in (300, 301, 302, 303, 307):
                location = response.headers['location']
                next_url = urllib.parse.urljoin(url, location)
                self.record_statistic(FetchStatistic(url=url,
                                                     next_url=next_url,
                                                     status=response.status,
                                                     exception=None,
                                                     size=0,
                                                     content_type=None,
                                                     encoding=None,
                                                     num_urls=0,
                                                     num_new_urls=0))

                if next_url in self.seen_urls:
                    return
                if max_redirect > 0:
                    LOGGER.info('redirect to %r from %r', next_url, url)
                    self.add_url(next_url, max_redirect - 1)
                else:
                    LOGGER.error('redirect limit reached for %r from %r',
                                 next_url, url)
            else:
                stat, links = await self.parse_links(response)
                self.record_statistic(stat)
                for link in links.difference(self.seen_urls):
                    self.q.put_nowait((link, max_redirect - 1))
                self.seen_urls.update(links)
        finally:
            await response.release()

    async def work(self):
        """Process queue items forever."""
        try:
            while True:
                url, max_redirect = await self.q.get()
                assert url in self.seen_urls
                await self.fetch(url, max_redirect)
                self.q.task_done()
        except asyncio.CancelledError:
            pass

    def url_allowed(self, url):
        if self.exclude and re.search(self.exclude, url):
            return False
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ('http', 'https'):
            LOGGER.debug('skipping non-http scheme in %r', url)
            return False
        host = urllib.parse.splitport(parts.netloc)[0]
        if not self.host_okay(host):
            LOGGER.debug('skipping non-root host in %r', url)
            return False
        return True

    def add_url(self, url, max_redirect=None):
        """Add a URL to the queue if not seen before."""
        if not max_redirect:
            max_redirect = self.max_redirect
        LOGGER.debug('adding %r %r', url, max_redirect)
        self.seen_urls.add(url)
        self.q.put_nowait((url, max_redirect))

    async def crawl(self):
        """Run the crawler until all finished."""
        async with aiohttp.ClientSession(loop=self.loop) as session:
            self.session = session
            self.t0 = time.time()
            workers = [asyncio.ensure_future(
                self.work(), loop=self.loop) for _ in range(self.max_tasks)]
            await self.q.join()
            self.t1 = time.time()
            for w in workers:
                w.cancel()

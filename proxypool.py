import requests
import subprocess


def get_proxy():
    """Get the proxy from the Redis server"""
    response = requests.get("http://127.0.0.1:5010/get/")
    json_response = response.json()
    proxy = json_response.get("proxy")
    return 'http://{}'.format(proxy)


def delete_proxy(proxy):
    """Drop the proxy that failed with 3 tries"""
    r = requests.get("http://127.0.0.1:5010/delete/?proxy={}".format(proxy))


if __name__ == '__main__':
    subprocess.check_call(["docker", "start", "proxy"])
    try:
        p = get_proxy()
        print('Proxy pool running...\n', p)
    except Exception as err:
        print(err)

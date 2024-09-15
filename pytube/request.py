"""Implements a simple wrapper around urlopen."""
import http.client
import json
import logging
import re
import socket
from functools import lru_cache
from urllib import parse
from urllib.error import URLError
from urllib.request import Request, urlopen

from pytube.exceptions import RegexMatchError, MaxRetriesExceeded
from pytube.helpers import regex_search
import random

logger = logging.getLogger(__name__)
default_range_size = 9437184  # 9MB


def _execute_request(
    url,
    method=None,
    headers=None,
    data=None,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT
):
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:90.0) Gecko/20100101 Firefox/90.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 6.3; WOW64; Trident/7.0; rv:11.0) like Gecko",
        "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
        "Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko",
        "Mozilla/5.0 (Windows NT 10.0; WOW64; rv:40.0) Gecko/20100101 Firefox/40.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_4) AppleWebKit/600.7.12 (KHTML, like Gecko) Version/8.0.7 Safari/600.7.12",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.10; rv:40.0) Gecko/20100101 Firefox/40.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_5) AppleWebKit/600.8.9 (KHTML, like Gecko) Version/7.1.8 Safari/537.85.17",
        "Mozilla/5.0 (iPad; CPU OS 8_4 like Mac OS X) AppleWebKit/600.1.4 (KHTML, like Gecko) Version/8.0 Mobile/12H143 Safari/600.1.4",
        "Mozilla/5.0 (iPad; CPU OS 8_3 like Mac OS X) AppleWebKit/600.1.4 (KHTML, like Gecko) Version/8.0 Mobile/12F69 Safari/600.1.4",
        "Mozilla/5.0 (Windows NT 6.1; rv:40.0) Gecko/20100101 Firefox/40.0",
        "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; WOW64; Trident/6.0)",
        "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)",
        "Mozilla/5.0 (Windows NT 6.3; WOW64; Trident/7.0; Touch; rv:11.0) like Gecko",
    ]
    base_headers = {
        "User-Agent": random.choice(user_agents),
        "accept-language": "en-US,en"
    }
    if headers:
        base_headers.update(headers)
    if data and not isinstance(data, bytes): # encode data for request
            data = bytes(json.dumps(data), encoding="utf-8")
    if url.lower().startswith("http"):
        request = Request(url, headers=base_headers, method=method, data=data)
    else:
        raise ValueError("Invalid URL")
    return urlopen(request, timeout=timeout)  # nosec


def get(url, extra_headers=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http GET request.

    :param str url:
        The URL to perform the GET request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    if extra_headers is None:
        extra_headers = {}
    response = _execute_request(url, headers=extra_headers, timeout=timeout)
    return response.read().decode("utf-8")


def post(url, extra_headers=None, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http POST request.

    :param str url:
        The URL to perform the POST request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :param dict data:
        The data to send on the POST request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    # could technically be implemented in get,
    # but to avoid confusion implemented like this
    if extra_headers is None:
        extra_headers = {}
    if data is None:
        data = {}
    # required because the youtube servers are strict on content type
    # raises HTTPError [400]: Bad Request otherwise
    extra_headers.update({"Content-Type": "application/json"})
    response = _execute_request(
        url,
        headers=extra_headers,
        data=data,
        timeout=timeout
    )
    return response.read().decode("utf-8")


def seq_stream(
            url,
            timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
            max_retries=0):

    """Read the response in sequence.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = f'{split_url.scheme}://{split_url.netloc}/{split_url.path}?'

    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys['sq'] = 0
    url = base_url + parse.urlencode(querys)

    segment_data = b''
    for chunk in stream(url, timeout=timeout, max_retries=max_retries):
        yield chunk
        segment_data += chunk

    # We can then parse the header to find the number of segments
    stream_info = segment_data.split(b'\r\n')
    segment_count_pattern = re.compile(b'Segment-Count: (\\d+)')
    for line in stream_info:
        match = segment_count_pattern.search(line)
        if match:
            segment_count = int(match.group(1).decode('utf-8'))

    # We request these segments sequentially to build the file.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys['sq'] = seq_num
        url = base_url + parse.urlencode(querys)

        yield from stream(url, timeout=timeout, max_retries=max_retries)
        seq_num += 1
    return  # pylint: disable=R1711


# TODO: Refactor this code
def stream(url,
           timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
           max_retries=0):
    """Read the response in chunks.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    file_size: int = default_range_size  # fake filesize to start
    downloaded = 0
    while downloaded < file_size:
        stop_pos = min(downloaded + default_range_size, file_size) - 1
        range_header = f"bytes={downloaded}-{stop_pos}"
        tries = 0

        # Attempt to make the request multiple times as necessary.
        while True:
            # If the max retries is exceeded, raise an exception
            if tries >= 1 + max_retries:
                raise MaxRetriesExceeded()

            # Try to execute the request, ignoring socket timeouts
            try:
                response = _execute_request(
                    f"{url}&range={downloaded}-{stop_pos}",
                    method="GET",
                    timeout=timeout
                )
            except URLError as e:
                # We only want to skip over timeout errors, and
                # raise any other URLError exceptions
                if not isinstance(e.reason, socket.timeout):
                    raise
            except http.client.IncompleteRead:
                # Allow retries on IncompleteRead errors for unreliable connections
                pass
            else:
                # On a successful request, break from loop
                break
            tries += 1

        if file_size == default_range_size:
            try:
                resp = _execute_request(
                    f"{url}&range=0-99999999999",
                    method="GET",
                    timeout=timeout
                )
                content_range = resp.info()["Content-Length"]
                file_size = int(content_range)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(e)
        while True:
            try:
                chunk = response.read()
            except StopIteration:
                return

            if not chunk:
                break

            if chunk: downloaded += len(chunk)
            yield chunk
    return  # pylint: disable=R1711


@lru_cache()
def filesize(url):
    """Fetch size in bytes of file at given URL

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    return int(head(url)["content-length"])


@lru_cache()
def seq_filesize(url):
    """Fetch size in bytes of file at given URL from sequential requests

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    total_filesize = 0
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = f'{split_url.scheme}://{split_url.netloc}/{split_url.path}?'
    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys['sq'] = 0
    url = base_url + parse.urlencode(querys)
    response = _execute_request(
        url, method="GET"
    )

    response_value = response.read()
    # The file header must be added to the total filesize
    total_filesize += len(response_value)

    # We can then parse the header to find the number of segments
    segment_count = 0
    stream_info = response_value.split(b'\r\n')
    segment_regex = b'Segment-Count: (\\d+)'
    for line in stream_info:
        # One of the lines should contain the segment count, but we don't know
        #  which, so we need to iterate through the lines to find it
        try:
            segment_count = int(regex_search(segment_regex, line, 1))
        except RegexMatchError:
            pass

    if segment_count == 0:
        raise RegexMatchError('seq_filesize', segment_regex)

    # We make HEAD requests to the segments sequentially to find the total filesize.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys['sq'] = seq_num
        url = base_url + parse.urlencode(querys)

        total_filesize += int(head(url)['content-length'])
        seq_num += 1
    return total_filesize


def head(url):
    """Fetch headers returned http GET request.

    :param str url:
        The URL to perform the GET request for.
    :rtype: dict
    :returns:
        dictionary of lowercase headers
    """
    response_headers = _execute_request(url, method="HEAD").info()
    return {k.lower(): v for k, v in response_headers.items()}

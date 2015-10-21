# Copyright (c) 2010-2011 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
Cloud Files client library used internally
"""
from httplib import HTTPConnection
from httplib import HTTPException, HTTPSConnection
from time import sleep
from urllib import quote as _quote
from urlparse import urlparse, urlunparse
import errno
import logging
import socket

from lunr.common import config, exc

LOGGER = logging.getLogger('lunr.client.swift')


# Very heavy handed solution to getting read/write socket timeouts with
# swiftclient. This solves snapshots hanging due to network problems between
# us and swift. FIXME(cory) In the future, a better swiftclient can set the
# timeout value on each socket it uses instead.
socket.setdefaulttimeout(240)


def quote(value, safe='/'):
    """
    Patched version of urllib.quote that encodes utf8 strings before quoting
    """
    if isinstance(value, unicode):
        value = value.encode('utf8')
    return _quote(value, safe)


# look for a real json parser first
try:
    # simplejson is popular and pretty good
    from simplejson import loads as json_loads, dumps as json_dumps
except ImportError:
    from json import loads as json_loads, dumps as json_dumps


class ClientException(exc.ClientException):

    def __init__(self, msg, http_scheme='', http_host='', http_port='',
                 http_path='', http_query='', http_status=0, http_reason='',
                 http_device=''):
        Exception.__init__(self, msg)
        self.msg = msg
        self.http_scheme = http_scheme
        self.http_host = http_host
        self.http_port = http_port
        self.http_path = http_path
        self.http_query = http_query
        self.http_status = http_status
        self.http_reason = http_reason
        self.http_device = http_device

    def __str__(self):
        a = self.msg
        b = ''
        if self.http_scheme:
            b += '%s://' % self.http_scheme
        if self.http_host:
            b += self.http_host
        if self.http_port:
            b += ':%s' % self.http_port
        if self.http_path:
            b += self.http_path
        if self.http_query:
            b += '?%s' % self.http_query
        if self.http_status:
            if b:
                b = '%s %s' % (b, self.http_status)
            else:
                b = str(self.http_status)
        if self.http_reason:
            if b:
                b = '%s %s' % (b, self.http_reason)
            else:
                b = '- %s' % self.http_reason
        if self.http_device:
            if b:
                b = '%s: device %s' % (b, self.http_device)
            else:
                b = 'device %s' % self.http_device
        return b and '%s: %s' % (a, b) or a


def http_connection(url):
    """
    Make an HTTPConnection or HTTPSConnection

    :param url: url to connect to
    :returns: tuple of (parsed url, connection object)
    :raises ClientException: Unable to handle protocol scheme
    """
    parsed = urlparse(url)
    if parsed.scheme == 'http':
        conn = HTTPConnection(parsed.netloc)
    elif parsed.scheme == 'https':
        conn = HTTPSConnection(parsed.netloc)
    else:
        raise ClientException('Cannot handle protocol scheme %s for url %s' %
                              (parsed.scheme, repr(url)))
    return parsed, conn


def get_auth(url, user, key, region, snet=False):
    """
    Get authentication/authorization credentials.

    The snet parameter is used for Rackspace's ServiceNet internal network
    implementation. In this function, it simply adds *snet-* to the beginning
    of the host name for the returned storage URL. With Rackspace Cloud Files,
    use of this network path causes no bandwidth charges but requires the
    client to be running on Rackspace's ServiceNet network.

    :param url: authentication/authorization URL
    :param user: user to authenticate as
    :param key: key or password for authorization
    :param region: service region [dfw, ord, syd, iad, etc]
    :param snet: use SERVICENET internal network (see above), default is False
    :returns: tuple of (storage URL, auth token)
    :raises ClientException: HTTP GET request to auth URL failed
    """
    swift_service = 'object-store'
    parsed, conn = http_connection(url)
    params = json_dumps({"auth": {"RAX-KSKEY:apiKeyCredentials":
                                  {"username": user, "apiKey": key}}})
    conn.request('POST', parsed.path, params,
                 {'Accept': 'application/json',
                  'Content-Type': 'application/json'})
    resp = conn.getresponse()
    data = json_loads(resp.read())
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Auth POST failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port,
            http_path=parsed.path, http_status=resp.status,
            http_reason=resp.reason)

    try:
        token = data['access']['token']['id']
        for service in data['access']['serviceCatalog']:
            if service['type'] == swift_service:
                for points in service['endpoints']:
                    if points['region'] == region:
                        if snet:
                            storage_url = points['internalURL']
                        else:
                            storage_url = points['publicURL']
                        return storage_url, token
                raise ClientException('Region %s not found' % region)
        raise ClientException('Service Type %s not found' % swift_service)
    except KeyError:
        raise ClientException(
            'Inconsistent Service Catalog back from auth: %s' % data)


def get_account(url, token, marker=None, limit=None, prefix=None,
                http_conn=None, full_listing=False):
    """
    Get a listing of containers for the account.

    :param url: storage URL
    :param token: auth token
    :param marker: marker query
    :param limit: limit query
    :param prefix: prefix query
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :param full_listing: if True, return a full listing, else returns a max
                         of 10000 listings
    :returns: a tuple of (response headers, a list of containers) The response
              headers will be a dict and all header names will be lowercase.
    :raises ClientException: HTTP GET request failed
    """
    if not http_conn:
        http_conn = http_connection(url)
    if full_listing:
        rv = get_account(url, token, marker, limit, prefix, http_conn)
        listing = rv[1]
        while listing:
            marker = listing[-1]['name']
            listing = \
                get_account(url, token, marker, limit, prefix, http_conn)[1]
            if listing:
                rv[1].extend(listing)
        return rv
    parsed, conn = http_conn
    qs = 'format=json'
    if marker:
        qs += '&marker=%s' % quote(marker)
    if limit:
        qs += '&limit=%d' % limit
    if prefix:
        qs += '&prefix=%s' % quote(prefix)
    conn.request('GET', '%s?%s' % (parsed.path, qs), '',
                 {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    if resp.status < 200 or resp.status >= 300:
        resp.read()
        raise ClientException(
            'Account GET failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port,
            http_path=parsed.path, http_query=qs, http_status=resp.status,
            http_reason=resp.reason)
    if resp.status == 204:
        resp.read()
        return resp_headers, []
    return resp_headers, json_loads(resp.read())


def head_account(url, token, http_conn=None):
    """
    Get account stats.

    :param url: storage URL
    :param token: auth token
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: a dict containing the response's headers (all header names will
              be lowercase)
    :raises ClientException: HTTP HEAD request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    conn.request('HEAD', parsed.path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Account HEAD failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port,
            http_path=parsed.path, http_status=resp.status,
            http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers


def post_account(url, token, headers, http_conn=None):
    """
    Update an account's metadata.

    :param url: storage URL
    :param token: auth token
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP POST request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    headers['X-Auth-Token'] = token
    conn.request('POST', parsed.path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Account POST failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_status=resp.status,
            http_reason=resp.reason)


def get_container(url, token, container, marker=None, limit=None,
                  prefix=None, delimiter=None, http_conn=None,
                  full_listing=False):
    """
    Get a listing of objects for the container.

    :param url: storage URL
    :param token: auth token
    :param container: container name to get a listing for
    :param marker: marker query
    :param limit: limit query
    :param prefix: prefix query
    :param delimeter: string to delimit the queries on
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :param full_listing: if True, return a full listing, else returns a max
                         of 10000 listings
    :returns: a tuple of (response headers, a list of objects) The response
              headers will be a dict and all header names will be lowercase.
    :raises ClientException: HTTP GET request failed
    """
    if not http_conn:
        http_conn = http_connection(url)
    if full_listing:
        rv = get_container(url, token, container, marker, limit, prefix,
                           delimiter, http_conn)
        listing = rv[1]
        while listing:
            if not delimiter:
                marker = listing[-1]['name']
            else:
                marker = listing[-1].get('name', listing[-1].get('subdir'))
            listing = get_container(url, token, container, marker, limit,
                                    prefix, delimiter, http_conn)[1]
            if listing:
                rv[1].extend(listing)
        return rv
    parsed, conn = http_conn
    path = '%s/%s' % (parsed.path, quote(container))
    qs = 'format=json'
    if marker:
        qs += '&marker=%s' % quote(marker)
    if limit:
        qs += '&limit=%d' % limit
    if prefix:
        qs += '&prefix=%s' % quote(prefix)
    if delimiter:
        qs += '&delimiter=%s' % quote(delimiter)
    conn.request('GET', '%s?%s' % (path, qs), '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    if resp.status < 200 or resp.status >= 300:
        resp.read()
        raise ClientException(
            'Container GET failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_query=qs,
            http_status=resp.status, http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    if resp.status == 204:
        resp.read()
        return resp_headers, []
    return resp_headers, json_loads(resp.read())


def head_container(url, token, container, http_conn=None):
    """
    Get container stats.

    :param url: storage URL
    :param token: auth token
    :param container: container name to get stats for
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: a dict containing the response's headers (all header names will
              be lowercase)
    :raises ClientException: HTTP HEAD request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    conn.request('HEAD', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Container HEAD failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_status=resp.status,
            http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers


def put_container(url, token, container, headers=None, http_conn=None):
    """
    Create a container

    :param url: storage URL
    :param token: auth token
    :param container: container name to create
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP PUT request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    if not headers:
        headers = {}
    headers['X-Auth-Token'] = token
    conn.request('PUT', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Container PUT failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_status=resp.status,
            http_reason=resp.reason)


def post_container(url, token, container, headers, http_conn=None):
    """
    Update a container's metadata.

    :param url: storage URL
    :param token: auth token
    :param container: container name to update
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP POST request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    headers['X-Auth-Token'] = token
    conn.request('POST', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Container POST failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_status=resp.status,
            http_reason=resp.reason)


def delete_container(url, token, container, http_conn=None):
    """
    Delete a container

    :param url: storage URL
    :param token: auth token
    :param container: container name to delete
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP DELETE request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    conn.request('DELETE', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Container DELETE failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_status=resp.status,
            http_reason=resp.reason)


def get_object(url, token, container, name, http_conn=None,
               resp_chunk_size=None, newest=False):
    """
    Get an object

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to get
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :param resp_chunk_size: if defined, chunk size of data to read. NOTE: If
                            you specify a resp_chunk_size you must fully read
                            the object's contents before making another
                            request.
    :param newest:  Defaults to False if True then the header "x-newest = True"
                    is sent which will do a head on all three copies and make
                    sure the newest one is returned.
    :returns: a tuple of (response headers, the object's contents) The response
              headers will be a dict and all header names will be lowercase.
    :raises ClientException: HTTP GET request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    headers = {'X-Auth-Token': token}
    if newest:
        headers['X-Newest'] = 'True'
    conn.request('GET', path, '', headers)
    resp = conn.getresponse()
    if resp.status < 200 or resp.status >= 300:
        resp.read()
        raise ClientException(
            'Object GET failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port, http_path=path,
            http_status=resp.status, http_reason=resp.reason)
    if resp_chunk_size:

        def _object_body():
            buf = resp.read(resp_chunk_size)
            while buf:
                yield buf
                buf = resp.read(resp_chunk_size)
        object_body = _object_body()
    else:
        object_body = resp.read()
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers, object_body


def head_object(url, token, container, name, http_conn=None):
    """
    Get object info

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to get info for
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: a dict containing the response's headers (all header names will
              be lowercase)
    :raises ClientException: HTTP HEAD request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    conn.request('HEAD', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Object HEAD failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port, http_path=path,
            http_status=resp.status, http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers


def put_object(url, token, container, name, contents, content_length=None,
               etag=None, chunk_size=65536, content_type=None, headers=None,
               http_conn=None):
    """
    Put an object

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to put
    :param contents: a string or a file like object to read object data from
    :param content_length: value to send as content-length header; also limits
                           the amount read from contents
    :param etag: etag of contents
    :param chunk_size: chunk size of data to write
    :param content_type: value to send as content-type header
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: etag from server response
    :raises ClientException: HTTP PUT request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    if not headers:
        headers = {}
    headers['X-Auth-Token'] = token
    if etag:
        headers['ETag'] = etag.strip('"')
    if content_length is not None:
        headers['Content-Length'] = str(content_length)
    if content_type is not None:
        headers['Content-Type'] = content_type
    if not contents:
        headers['Content-Length'] = '0'
    if hasattr(contents, 'read'):
        conn.putrequest('PUT', path)
        for header, value in headers.iteritems():
            conn.putheader(header, value)
        if content_length is None:
            conn.putheader('Transfer-Encoding', 'chunked')
            conn.endheaders()
            chunk = contents.read(chunk_size)
            while chunk:
                conn.send('%x\r\n%s\r\n' % (len(chunk), chunk))
                chunk = contents.read(chunk_size)
            conn.send('0\r\n\r\n')
        else:
            conn.endheaders()
            left = content_length
            while left > 0:
                size = chunk_size
                if size > left:
                    size = left
                chunk = contents.read(size)
                conn.send(chunk)
                left -= len(chunk)
    else:
        conn.request('PUT', path, contents, headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Object PUT failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port, http_path=path,
            http_status=resp.status, http_reason=resp.reason)
    return resp.getheader('etag').strip('"')


def post_object(url, token, container, name, headers, http_conn=None):
    """
    Update object metadata

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: name of the object to update
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP POST request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    headers['X-Auth-Token'] = token
    conn.request('POST', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Object POST failed', http_scheme=parsed.scheme,
            http_host=conn.host, http_port=conn.port, http_path=path,
            http_status=resp.status, http_reason=resp.reason)


def delete_object(url, token, container, name, headers=None, http_conn=None):
    """
    Delete object

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to delete
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP DELETE request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    headers = headers or {}
    headers['X-Auth-Token'] = token
    conn.request('DELETE', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException(
            'Object DELETE failed',
            http_scheme=parsed.scheme, http_host=conn.host,
            http_port=conn.port, http_path=path, http_status=resp.status,
            http_reason=resp.reason)


class Connection(object):
    """Convenience class to make requests that will also retry the request"""

    def __init__(self, authurl, user, key, region, retries=5, preauthurl=None,
                 preauthtoken=None, snet=False, starting_backoff=1):
        """
        :param authurl: authenitcation URL
        :param user: user name to authenticate as
        :param key: key/password to authenticate with
        :param retries: Number of times to retry the request before failing
        :param preauthurl: storage URL (if you have already authenticated)
        :param preauthtoken: authentication token (if you have already
                             authenticated)
        :param snet: use SERVICENET internal network default is False
        """
        self.authurl = authurl
        self.user = user
        self.key = key
        self.region = region
        self.retries = retries
        self.http_conn = None
        self.url = preauthurl
        self.token = preauthtoken
        self.snet = snet
        self.starting_backoff = starting_backoff

    def get_auth(self):
        return get_auth(self.authurl, self.user, self.key, self.region,
                        snet=self.snet)

    def http_connection(self):
        return http_connection(self.url)

    def _retry(self, reset_func, func, *args, **kwargs):
        auth_attempts, attempts = 0, 0
        backoff = self.starting_backoff
        while attempts <= self.retries:
            attempts += 1
            try:
                if not self.url or not self.token:
                    self.url, self.token = self.get_auth()
                    self.http_conn = None
                if not self.http_conn:
                    self.http_conn = self.http_connection()
                kwargs['http_conn'] = self.http_conn
                rv = func(self.url, self.token, *args, **kwargs)
                return rv
            except (socket.error, HTTPException), e:
                LOGGER.debug(
                    '%s resetting connection (%s)' % (e, id(self.http_conn)))
                if hasattr(self.http_conn, 'netloc'):
                    netloc = self.http_conn.netloc
                else:
                    netloc = urlparse(self.url or self.authurl).netloc
                    host = netloc.rsplit(':', 1)[0]
                self.http_conn = None
                if attempts > self.retries:
                    if isinstance(e, socket.gaierror) and e.errno == -2:
                        raise ClientException("Could not resolve '%s'" % host)
                    if isinstance(e, socket.error) and \
                            e.errno == errno.ECONNREFUSED:
                        raise ClientException("Connection refused from '%s'" %
                                              netloc)
                    LOGGER.exception('Unexpected socket/http error')
                    raise ClientException('Unable to make request: %s' % e)
            except ClientException, err:
                if attempts > self.retries:
                    raise
                if not 500 <= err.http_status <= 599:
                    if err.http_status == 401:
                        # retry auth once after initial auth failure
                        if auth_attempts >= 1:
                            raise
                        self.http_conn = self.url = self.token = None
                        auth_attempts += 1
                    elif err.http_status == 408:
                        # Try again on '408 Request TimeOut'
                        self.http_conn = None
                    else:
                        raise
            LOGGER.warning('attempt #%d failed, retrying in %d seconds' %
                           (attempts, backoff), exc_info=True)
            sleep(backoff)
            backoff *= 2
            if reset_func:
                reset_func(func, *args, **kwargs)

    def head_account(self):
        """Wrapper for :func:`head_account`"""
        return self._retry(None, head_account)

    def get_account(self, marker=None, limit=None, prefix=None,
                    full_listing=False):
        """Wrapper for :func:`get_account`"""
        # TODO(unknown): With full_listing=True this will restart the entire
        # listing with each retry. Need to make a better version that just
        # retries where it left off.
        return self._retry(None, get_account, marker=marker, limit=limit,
                           prefix=prefix, full_listing=full_listing)

    def post_account(self, headers):
        """Wrapper for :func:`post_account`"""
        return self._retry(None, post_account, headers)

    def head_container(self, container):
        """Wrapper for :func:`head_container`"""
        return self._retry(None, head_container, container)

    def get_container(self, container, marker=None, limit=None, prefix=None,
                      delimiter=None, full_listing=False):
        """Wrapper for :func:`get_container`"""
        # TODO(unknown): With full_listing=True this will restart the entire
        # listing with each retry. Need to make a better version that just
        # retries where it left off.
        return self._retry(None, get_container, container, marker=marker,
                           limit=limit, prefix=prefix, delimiter=delimiter,
                           full_listing=full_listing)

    def put_container(self, container, headers=None):
        """Wrapper for :func:`put_container`"""
        return self._retry(None, put_container, container, headers=headers)

    def post_container(self, container, headers):
        """Wrapper for :func:`post_container`"""
        return self._retry(None, post_container, container, headers)

    def delete_container(self, container):
        """Wrapper for :func:`delete_container`"""
        return self._retry(None, delete_container, container)

    def head_object(self, container, obj):
        """Wrapper for :func:`head_object`"""
        return self._retry(None, head_object, container, obj)

    def get_object(self, container, obj, resp_chunk_size=None, newest=False):
        """Wrapper for :func:`get_object`"""
        return self._retry(None, get_object, container, obj,
                           resp_chunk_size=resp_chunk_size,
                           newest=newest)

    def put_object(self, container, obj, contents, content_length=None,
                   etag=None, chunk_size=65536, content_type=None,
                   headers=None):
        """Wrapper for :func:`put_object`"""

        def _default_reset(*args, **kwargs):
            raise ClientException(
                'put_object(%r, %r, ...) failure and no '
                'ability to reset contents for reupload.' % (container, obj))

        reset_func = None
        if hasattr(contents, 'read'):
            reset_func = _default_reset
            tell = getattr(contents, 'tell', None)
            seek = getattr(contents, 'seek', None)
            if tell and seek:
                orig_pos = tell()
                reset_func = lambda *a, **k: seek(orig_pos)

        return self._retry(
            reset_func, put_object, container, obj, contents,
            content_length=content_length, etag=etag, chunk_size=chunk_size,
            content_type=content_type, headers=headers)

    def post_object(self, container, obj, headers):
        """Wrapper for :func:`post_object`"""
        return self._retry(None, post_object, container, obj, headers)

    def delete_object(self, container, obj, headers=None):
        """Wrapper for :func:`delete_object`"""
        return self._retry(None, delete_object, container, obj,
                           headers=headers)


def connect(conf):
    auth_url = conf.string('swift', 'auth_url',
                           'http://localhost:8082/auth/v1.0')
    user = conf.string('swift', 'user', 'test:tester')
    key = conf.string('swift', 'key', 'testing')
    region = conf.string('swift', 'region', 'USA')
    snet = conf.bool('swift', 'snet', False)
    retries = conf.int('swift', 'retries', 5)
    return Connection(auth_url, user, key, region, retries=retries, snet=snet)

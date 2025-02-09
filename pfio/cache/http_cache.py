import logging
import os
import pickle
import time
from typing import Any, Optional

import urllib3
import urllib3.exceptions

from pfio.cache import Cache

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())


class _ConnectionPool(object):
    def __init__(self, retries: int, timeout: int):
        self.retries = retries
        self.timeout = timeout

        self.conn: Optional[urllib3.poolmanager.PoolManager] = None
        self.pid: Optional[int] = None

    def __getstate__(self):
        state = self.__dict__.copy()
        state['conn'] = None
        return state

    def __setstate__(self, state):
        self.__dict__ = state

    @property
    def is_forked(self):
        return self.pid != os.getpid()

    def urlopen(self, method, url, redirect=True, **kw):
        if self.is_forked or self.conn is None:
            self.conn = urllib3.poolmanager.PoolManager(
                retries=self.retries,
                timeout=self.timeout
            )
            self.pid = os.getpid()
        return self.conn.urlopen(method, url, redirect, **kw)


CONNECTION_POOL: Optional[_ConnectionPool] = None


def _get_connection_pool(retries: int, timeout: int) -> _ConnectionPool:
    global CONNECTION_POOL
    if CONNECTION_POOL is None:
        CONNECTION_POOL = _ConnectionPool(retries, timeout)
    return CONNECTION_POOL


class HTTPConnector(object):
    def __init__(self,
                 url: str,
                 bearer_token_path: Optional[str] = None,
                 retries: int = 1,
                 timeout: int = 3):
        if url.endswith("/"):
            self.url = url
        else:
            self.url = url + "/"

        self.bearer_token_path: Optional[str] = None
        if bearer_token_path is not None:
            self.bearer_token_path = bearer_token_path
        else:
            self.bearer_token_path = os.getenv("PFIO_HTTP_BEARER_TOKEN_PATH")

        if self.bearer_token_path is not None:
            self._token_read_now()

        # Allow redirect or retry once by default
        self.conn = _get_connection_pool(retries, timeout)

    def put(self, suffix: str, data: bytes) -> bool:
        try:
            res = self.conn.urlopen("PUT",
                                    url=self.url + suffix,
                                    headers=self._header_with_token(),
                                    body=data)
        except urllib3.exceptions.RequestError as e:
            logger.warning("put: {}".format(e))
            return False

        if res.status == 201:
            return True
        else:
            logger.warning("put: unexpected status code {}".format(res.status))
            return False

    def get(self, suffix: str) -> Optional[bytes]:
        try:
            res = self.conn.urlopen("GET",
                                    url=self.url + suffix,
                                    headers=self._header_with_token())
        except urllib3.exceptions.RequestError as e:
            logger.warning("get: {}".format(e))
            return None

        if res.status == 200:
            return res.data
        elif res.status == 404:
            return None
        else:
            logger.warning("get: unexpected status code {}".format(res.status))
            return None

    def _header_with_token(self) -> dict:
        if self.bearer_token_path is None:
            return {}
        else:
            if time.time() - self.bearer_token_updated > 1:
                self._token_read_now()
            return {
                "Authorization": f"Bearer {self.bearer_token}"
            }

    def _token_read_now(self):
        with open(self.bearer_token_path, "r") as f:
            self.bearer_token = f.read()
            self.bearer_token_updated = time.time()


class HTTPCache(Cache):
    """HTTP-based cache system

    Stores cache data in an HTTP server with ``PUT`` and ``GET`` methods. Each
    cache entry corresponds to url suffixed by index ``i``.

    Arguments:
        length (int):
            Length of the cache.

        url (string):
            Prefix url of cache entries. Each entry corresponds to the url
            suffixed by each index. A user must specify url as globally
            identical across the cache system in the server side, because
            ``HTTPCache`` doesn't suffix the url by user or dataset
            information. Therefore, a user should include user and dataset in
            the url to avoid conflicting the cache entry.

            For example, let's assume that given url is
            ``http://cache.example.com/some/{user}/{dataset-id}/``. Here,
            ``put(123)`` and ``get(123)`` correspond to
            ``http://cache.example.com/some/{user}/{dataset-id}/123``.

        bearer_token_path (string):
            Path to HTTP bearer token if authorization required. ``HTTPCache``
            supports refresh of bearer token by periodical reloading.

        do_pickle (bool):
            Do automatic pickle and unpickle inside the cache.

    .. note:: This feature is experimental.

    """

    def __init__(self,
                 length: int,
                 url: str,
                 bearer_token_path=None,
                 do_pickle=False):
        super().__init__()

        self.length = length
        assert self.length > 0

        self.connector = HTTPConnector(url, bearer_token_path)
        self.do_pickle = do_pickle

    def __len__(self):
        return self.length

    @property
    def multiprocess_safe(self):
        return True

    @property
    def multithread_safe(self):
        return True

    def put(self, i: int, data: Any):
        if i < 0 or self.length <= i:
            raise IndexError("index {} out of range ([0, {}])"
                             .format(i, self.length - 1))
        if self.do_pickle:
            data = pickle.dumps(data)

        return self.connector.put(str(i), data)

    def get(self, i: int) -> Any:
        if i < 0 or self.length <= i:
            raise IndexError("index {} out of range ([0, {}])"
                             .format(i, self.length - 1))

        data = self.connector.get(str(i))

        if self.do_pickle and data is not None:
            return pickle.loads(data)
        else:
            return data

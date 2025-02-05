import asyncio
import errno
import json
from json import (
    JSONDecodeError,
)
import logging
from pathlib import (
    Path,
)
import sys
from typing import (
    Any,
    Optional,
    Tuple,
    Union,
)

from eth_utils import (
    to_text,
)

from web3._utils.caching import (
    async_handle_request_caching,
    generate_cache_key,
)
from web3.exceptions import (
    ProviderConnectionError,
    TimeExhausted,
)
from web3.types import (
    RPCEndpoint,
    RPCId,
    RPCResponse,
)

from . import (
    PersistentConnectionProvider,
)
from ..ipc import (
    get_default_ipc_path,
)


async def async_get_ipc_socket(
    ipc_path: str,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if sys.platform == "win32":
        # On Windows named pipe is used. Simulate socket with it.
        from web3._utils.windows import (
            NamedPipe,
        )

        return NamedPipe(ipc_path)
    else:
        return await asyncio.open_unix_connection(ipc_path)


class AsyncIPCProvider(PersistentConnectionProvider):
    logger = logging.getLogger("web3.providers.AsyncIPCProvider")

    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None

    def __init__(
        self,
        ipc_path: Optional[Union[str, Path]] = None,
        request_timeout: int = 10,
        max_connection_retries: int = 5,
        **kwargs: Any,
    ) -> None:
        if ipc_path is None:
            self.ipc_path = get_default_ipc_path()
        elif isinstance(ipc_path, str) or isinstance(ipc_path, Path):
            self.ipc_path = str(Path(ipc_path).expanduser().resolve())
        else:
            raise TypeError("ipc_path must be of type string or pathlib.Path")

        self.request_timeout = request_timeout
        self._max_connection_retries = max_connection_retries
        super().__init__(request_timeout, max_connection_retries, **kwargs)

    def __str__(self) -> str:
        return f"<{self.__class__.__name__} {self.ipc_path}>"

    async def is_connected(self, show_traceback: bool = False) -> bool:
        try:
            await self.make_request(RPCEndpoint("web3_clientVersion"), [])
            return True
        except (OSError, BrokenPipeError, ProviderConnectionError) as e:
            if show_traceback:
                raise ProviderConnectionError(
                    f"Problem connecting to provider with error: {type(e)}: {e}"
                )
            return False

    async def connect(self) -> None:
        _connection_attempts = 0
        _backoff_rate_change = 1.75
        _backoff_time = 1.75

        while _connection_attempts != self._max_connection_retries:
            try:
                _connection_attempts += 1
                self.reader, self.writer = await async_get_ipc_socket(self.ipc_path)
                self._message_listener_task = asyncio.create_task(
                    self._message_listener()
                )
                break
            except OSError as e:
                if _connection_attempts == self._max_connection_retries:
                    raise ProviderConnectionError(
                        f"Could not connect to endpoint: {self.endpoint_uri}. "
                        f"Retries exceeded max of {self._max_connection_retries}."
                    ) from e
                self.logger.info(
                    f"Could not connect to endpoint: {self.endpoint_uri}. Retrying in "
                    f"{round(_backoff_time, 1)} seconds.",
                    exc_info=True,
                )
                await asyncio.sleep(_backoff_time)
                _backoff_time *= _backoff_rate_change

    async def disconnect(self) -> None:
        if self.writer and not self.writer.is_closing():
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            self.logger.debug(
                f'Successfully disconnected from endpoint: "{self.endpoint_uri}'
            )

        try:
            self._message_listener_task.cancel()
            await self._message_listener_task
            self.reader = None
        except (asyncio.CancelledError, StopAsyncIteration):
            pass

        self._request_processor.clear_caches()

    async def _reset_socket(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()
        self.reader, self.writer = await async_get_ipc_socket(self.ipc_path)

    @async_handle_request_caching
    async def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        request_data = self.encode_rpc_request(method, params)

        if self.writer is None:
            raise ProviderConnectionError(
                "Connection to ipc socket has not been initiated for the provider."
            )

        try:
            self.writer.write(request_data)
            await self.writer.drain()
        except OSError as e:
            # Broken pipe
            if e.errno == errno.EPIPE:
                # one extra attempt, then give up
                await self._reset_socket()
                self.writer.write(request_data)
                await self.writer.drain()

        current_request_id = json.loads(request_data)["id"]
        response = await self._get_response_for_request_id(current_request_id)

        return response

    async def _get_response_for_request_id(self, request_id: RPCId) -> RPCResponse:
        async def _match_response_id_to_request_id() -> RPCResponse:
            request_cache_key = generate_cache_key(request_id)

            while True:
                # sleep(0) here seems to be the most efficient way to yield control
                # back to the event loop while waiting for the response to be in the
                # queue.
                await asyncio.sleep(0)

                if request_cache_key in self._request_processor._request_response_cache:
                    self.logger.debug(
                        f"Popping response for id {request_id} from cache."
                    )
                    popped_response = self._request_processor.pop_raw_response(
                        cache_key=request_cache_key,
                    )
                    return popped_response

        try:
            # Add the request timeout around the while loop that checks the request
            # cache and tried to recv(). If the request is neither in the cache, nor
            # received within the request_timeout, raise ``TimeExhausted``.
            return await asyncio.wait_for(
                _match_response_id_to_request_id(), self.request_timeout
            )
        except asyncio.TimeoutError:
            raise TimeExhausted(
                f"Timed out waiting for response with request id `{request_id}` after "
                f"{self.request_timeout} second(s). This may be due to the provider "
                "not returning a response with the same id that was sent in the "
                "request or an exception raised during the request was caught and "
                "allowed to continue."
            )

    async def _message_listener(self) -> None:
        self.logger.info(
            "IPC socket listener background task started. Storing all messages in "
            "appropriate request processor queues / caches to be processed."
        )
        raw_message = ""
        decoder = json.JSONDecoder()

        while True:
            # the use of sleep(0) seems to be the most efficient way to yield control
            # back to the event loop to share the loop with other tasks.
            await asyncio.sleep(0)

            try:
                raw_message += to_text(await self.reader.read(4096)).lstrip()

                while raw_message:
                    try:
                        response, pos = decoder.raw_decode(raw_message)
                    except JSONDecodeError:
                        break

                    is_subscription = response.get("method") == "eth_subscription"
                    await self._request_processor.cache_raw_response(
                        response, subscription=is_subscription
                    )
                    raw_message = raw_message[pos:].lstrip()
            except Exception as e:
                if not self.silence_listener_task_exceptions:
                    loop = asyncio.get_event_loop()
                    for task in asyncio.all_tasks(loop=loop):
                        task.cancel()
                    raise e

                self.logger.error(
                    "Exception caught in listener, error logging and keeping listener "
                    f"background task alive.\n    error={e}"
                )
                # if only error logging, reset the ``raw_message`` buffer and continue
                raw_message = ""

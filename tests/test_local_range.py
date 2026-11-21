from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from goszakup import local_signer
from tools.sim_portal import VirtualLoop


BASE_URL = "https://portal.test"
FILE_PATH = "/files/download_file/123/456/"
REFERER = f"{BASE_URL}/ru/application/show_doc/11/22/3334/33/2"
ETAG = '"exact-version"'
PREFIX = b"first half"
SUFFIX = b"last half!"
PAYLOAD = PREFIX + SUFFIX
SIZE = len(PAYLOAD)
OFFSET = len(PREFIX)
FULL_HEADERS = {"Content-Length": str(SIZE), "Accept-Ranges": "bytes", "ETag": ETAG}
SUFFIX_HEADERS = {
    "Content-Length": str(len(SUFFIX)),
    "Content-Range": f"bytes {OFFSET}-{SIZE - 1}/{SIZE}",
    "ETag": ETAG,
}


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.chunks()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("ATAGOS_SPEC_RANGE_DOWNLOAD", "1")
    monkeypatch.setattr(local_signer, "_SPEC_RANGE_MIN_BYTES", 1)


async def sign(client, *, cache_key="fid:123"):
    kalkan = AsyncMock()
    kalkan.key_id = "certificate"
    kalkan.sign_bytes.return_value = "signature"
    signer = local_signer.LocalSigner(http_client=client, kalkan_signer=kalkan)
    signature = await signer.sign_file(BASE_URL + FILE_PATH, referer=REFERER, cache_key=cache_key)
    assert signature == "signature"
    kalkan.sign_bytes.assert_awaited_once_with(PAYLOAD)


async def test_valid_range_wins_and_cannot_rotate_active_cookie(enabled):
    async def original():
        yield PREFIX + SUFFIX[:2]  # the split need not align with an HTTP chunk
        await asyncio.Future()  # only the validated suffix can complete the sign

    original_stream = Stream(original)

    async def handler(request):
        assert request.url.path == FILE_PATH
        assert request.headers["referer"] == REFERER
        assert request.headers["cookie"] == "ci_session=active"
        if "range" not in request.headers:
            return httpx.Response(200, headers=FULL_HEADERS, stream=original_stream)
        assert request.headers["range"] == f"bytes={OFFSET}-{SIZE - 1}"
        assert request.headers["if-range"] == ETAG
        assert request.headers["accept-encoding"] == "identity"
        headers = {**SUFFIX_HEADERS, "Set-Cookie": "ci_session=speculative; Path=/"}
        return httpx.Response(206, headers=headers, content=SUFFIX)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url=BASE_URL, transport=transport,
                                 cookies={"ci_session": "active"}) as client:
        await asyncio.wait_for(sign(client), 1)
        assert client.cookies.get("ci_session") == "active"
    assert original_stream.closed


async def test_original_completion_cancels_a_stalled_range_immediately(enabled):
    range_started = asyncio.Event()
    range_cancelled = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def original():
        yield PREFIX
        await range_started.wait()
        yield SUFFIX

    async def handler(request):
        if "range" not in request.headers:
            return httpx.Response(200, headers=FULL_HEADERS, stream=Stream(original))
        range_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            range_cancelled.set()
            await allow_cleanup.wait()
            cleanup_done.set()
            raise

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        try:
            await asyncio.wait_for(sign(client), 1)
            await asyncio.wait_for(range_cancelled.wait(), 1)
            assert not cleanup_done.is_set()
        finally:
            allow_cleanup.set()
        await asyncio.wait_for(cleanup_done.wait(), 1)


@pytest.mark.parametrize("invalid", [
    {"status_code": 200},
    {"headers": {**SUFFIX_HEADERS, "ETag": '"different"'}},
    {"headers": {**SUFFIX_HEADERS, "Content-Range": f"bytes 0-{SIZE - 1}/{SIZE}"}},
    {"headers": {**SUFFIX_HEADERS, "Content-Length": "1"}},
    {"headers": {**SUFFIX_HEADERS, "Content-Encoding": "gzip"}},
    {"headers": {**SUFFIX_HEADERS, "Content-Type": "multipart/byteranges; boundary=x"}},
    {"content": b"x"},
    {"content": b"x" * (SIZE + 1)},
    httpx.ConnectError("auxiliary request failed"),
    RuntimeError("unexpected auxiliary transport failure"),
])
async def test_invalid_range_response_falls_back_to_original(enabled, invalid):
    range_finished = asyncio.Event()

    async def original():
        yield PREFIX
        await range_finished.wait()
        # The auxiliary result is ready first. Keep the original stream alive
        # while the downloader verifies whether it can use the suffix.
        await asyncio.sleep(0)
        yield SUFFIX

    async def handler(request):
        if "range" not in request.headers:
            return httpx.Response(200, headers=FULL_HEADERS, stream=Stream(original))
        range_finished.set()
        if isinstance(invalid, Exception):
            raise invalid
        parameters = {"status_code": 206, "headers": SUFFIX_HEADERS,
                      "content": b"CORRUPTED!", **invalid}
        response_body = parameters.pop("content")

        async def invalid_suffix():
            yield response_body

        return httpx.Response(stream=Stream(invalid_suffix), **parameters)

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        await asyncio.wait_for(sign(client), 1)
    assert range_finished.is_set()


@pytest.mark.parametrize("flag,cache_key,changes,min_bytes", [
    ("0", "fid:123", {}, 1),
    ("1", "fid:999", {}, 1),
    ("1", None, {}, 1),
    ("1", "fid:123", {}, 4 * 1024 * 1024),
    ("1", "fid:123", {"Accept-Ranges": "none"}, 1),
    ("1", "fid:123", {"ETag": 'W/"weak"'}, 1),
    ("1", "fid:123", {"ETag": "invalid"}, 1),
])
async def test_range_requires_opt_in_and_valid_metadata(monkeypatch, flag, cache_key, changes, min_bytes):
    monkeypatch.setenv("ATAGOS_SPEC_RANGE_DOWNLOAD", flag)
    monkeypatch.setattr(local_signer, "_SPEC_RANGE_MIN_BYTES", min_bytes)
    requests = []

    async def original():
        yield PAYLOAD

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={**FULL_HEADERS, **changes}, stream=Stream(original))

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        await sign(client, cache_key=cache_key)
    assert len(requests) == 1 and "range" not in requests[0].headers


async def test_buffered_original_needs_no_range(enabled):
    def handler(request):
        assert "range" not in request.headers
        return httpx.Response(200, headers=FULL_HEADERS, content=PAYLOAD)

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        await sign(client)


@pytest.mark.parametrize("body", [PREFIX[:-1], PAYLOAD + b"x"])
async def test_eligible_original_with_wrong_length_is_never_signed(enabled, body):
    async def original():
        yield body

    def handler(request):
        if "range" in request.headers:
            return httpx.Response(416)
        return httpx.Response(200, headers=FULL_HEADERS, stream=Stream(original))

    kalkan = AsyncMock()
    kalkan.key_id = "certificate"
    cache = {}
    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        signer = local_signer.LocalSigner(http_client=client, kalkan_signer=kalkan)
        with pytest.raises(httpx.ReadError, match="unexpected document length"):
            await signer.sign_file(BASE_URL + FILE_PATH, referer=REFERER, cache_key="fid:123", sig_cache=cache)
    kalkan.sign_bytes.assert_not_awaited()
    assert not cache


async def test_original_read_failure_before_prefix_is_preserved(enabled):
    range_started = asyncio.Event()
    range_cancelled = asyncio.Event()

    async def broken_original():
        yield PREFIX[:-1]
        await range_started.wait()
        raise httpx.ReadError("full download failed")

    async def handler(request):
        if "range" not in request.headers:
            return httpx.Response(200, headers=FULL_HEADERS, stream=Stream(broken_original))
        range_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            range_cancelled.set()
            raise

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadError, match="full download failed"):
            await asyncio.wait_for(sign(client), 1)
    assert range_cancelled.is_set()


def test_range_model_shaves_time_from_independent_streams(monkeypatch):
    monkeypatch.setattr(local_signer, "_SPEC_RANGE_MIN_BYTES", 1)

    async def download():
        async def original():
            await asyncio.sleep(1.9375)
            yield PREFIX
            await asyncio.sleep(1.9375)
            yield SUFFIX

        async def suffix():
            await asyncio.sleep(1.9375)
            yield SUFFIX

        async def handler(request):
            await asyncio.sleep(0.766)
            if "range" in request.headers:
                return httpx.Response(206, headers=SUFFIX_HEADERS, stream=Stream(suffix))
            return httpx.Response(200, headers=FULL_HEADERS, stream=Stream(original))

        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            await sign(client)

    seconds = []
    for flag in ("0", "1"):
        monkeypatch.setenv("ATAGOS_SPEC_RANGE_DOWNLOAD", flag)
        with asyncio.Runner(loop_factory=VirtualLoop) as runner:
            runner.run(download())
            seconds.append(runner.get_loop().time())
    assert seconds == pytest.approx([4.641, 3.4695])

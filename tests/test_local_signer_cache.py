from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from goszakup.local_signer import LocalSigner


BASE_URL = "https://portal.test"
FIRST_URL = f"{BASE_URL}/files/download_file/123/456/"
OTHER_TOKEN_URL = f"{BASE_URL}/files/download_file/123/789/"
PAYLOAD = b"exact specification data"


def _signer(client):
    kalkan = AsyncMock()
    kalkan.key_id = "certificate"
    kalkan.sign_bytes.return_value = "signature"
    return LocalSigner(http_client=client, kalkan_signer=kalkan), kalkan


async def test_concurrent_references_share_download_and_signature():
    downloaded = asyncio.Event()
    allow_body = asyncio.Event()
    requests = []
    cache = {}

    async def handler(request):
        requests.append(request.url.path)
        downloaded.set()
        await allow_body.wait()
        return httpx.Response(200, content=PAYLOAD)

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        signer, kalkan = _signer(client)
        first = asyncio.create_task(signer.sign_file(FIRST_URL, sig_cache=cache, cache_key="fid:123"))
        await asyncio.wait_for(downloaded.wait(), 1)
        second = asyncio.create_task(signer.sign_file(OTHER_TOKEN_URL, sig_cache=cache, cache_key="fid:123"))
        try:
            await asyncio.sleep(0)
        finally:
            allow_body.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), 1) == ["signature", "signature"]

    assert requests == ["/files/download_file/123/456/"]
    kalkan.sign_bytes.assert_awaited_once_with(PAYLOAD)


async def test_coalesced_reference_can_retry_after_first_download_is_cancelled():
    downloading = asyncio.Event()
    requests = []
    cache = {}

    async def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            downloading.set()
            await asyncio.Future()
        return httpx.Response(200, content=PAYLOAD)

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        signer, kalkan = _signer(client)
        first = asyncio.create_task(signer.sign_file(FIRST_URL, sig_cache=cache, cache_key="fid:123"))
        await asyncio.wait_for(downloading.wait(), 1)
        second = asyncio.create_task(signer.sign_file(OTHER_TOKEN_URL, sig_cache=cache, cache_key="fid:123"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.wait_for(second, 1) == "signature"

    assert requests == ["/files/download_file/123/456/", "/files/download_file/123/789/"]
    kalkan.sign_bytes.assert_awaited_once_with(PAYLOAD)


@pytest.mark.parametrize("separate_caches", [False, True])
async def test_concurrent_independent_references_do_not_wait_for_each_other(separate_caches):
    downloading = asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("/123/456/"):
            downloading.set()
            await asyncio.Future()
        return httpx.Response(200, content=PAYLOAD)

    cache = {}
    other_cache = {} if separate_caches else cache
    other_file = "123" if separate_caches else "124"
    other_url = f"{BASE_URL}/files/download_file/{other_file}/789/"

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        signer, kalkan = _signer(client)
        first = asyncio.create_task(signer.sign_file(FIRST_URL, sig_cache=cache, cache_key="fid:123"))
        try:
            await asyncio.wait_for(downloading.wait(), 1)
            result = await asyncio.wait_for(signer.sign_file(
                other_url, sig_cache=other_cache, cache_key=f"fid:{other_file}",
            ), 1)
            assert result == "signature"
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

    kalkan.sign_bytes.assert_awaited_once_with(PAYLOAD)

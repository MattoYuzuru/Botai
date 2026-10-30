"""Links to Google Docs, Sheets, Slides and Drive become public exports; a closed one is an honest
error. No real Google is called: the redirect shapes below are what its export addresses answer."""

from __future__ import annotations

import httpx
import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.sources import public_links
from botai.sources.adapter import EndpointOutcome
from botai.sources.adapters.document import DocumentAdapter
from botai.sources.factory import AdapterFactory
from tests.sources.helpers import make_ctx
from tests.sources.test_documents import make_xlsx

ID = "1AbCdEfGhIjKlMnOpQrStUv"
SHEET = f"https://docs.google.com/spreadsheets/d/{ID}/edit"
CONTENT_HOST = "doc-08-4o-sheets.googleusercontent.com"


@pytest.mark.parametrize(
    ("link", "export"),
    [
        (
            f"https://docs.google.com/document/d/{ID}/edit?usp=sharing",
            f"/document/d/{ID}/export?format=docx",
        ),
        (SHEET, f"/spreadsheets/d/{ID}/export?format=xlsx"),
        (f"{SHEET}#gid=77", f"/spreadsheets/d/{ID}/export?format=csv&gid=77"),
        (f"{SHEET}?gid=5#gid=77", f"/spreadsheets/d/{ID}/export?format=csv&gid=5"),
        (f"https://docs.google.com/presentation/d/{ID}/edit", f"/presentation/d/{ID}/export/pdf"),
        (
            f"https://docs.google.com/spreadsheets/d/e/2PACX-{ID}/pubhtml",
            f"/spreadsheets/d/e/2PACX-{ID}/pub?output=xlsx",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/e/2PACX-{ID}/pubhtml?gid=3",
            f"/spreadsheets/d/e/2PACX-{ID}/pub?output=csv&gid=3&single=true",
        ),
        (f"https://docs.google.com/document/d/e/2PACX-{ID}/pub", f"/document/d/e/2PACX-{ID}/pub"),
    ],
)
def test_google_editor_links_become_public_export_addresses(link: str, export: str) -> None:
    assert public_links.export_url(link) == f"https://docs.google.com{export}"


@pytest.mark.parametrize(
    "link",
    [
        f"https://drive.google.com/file/d/{ID}/view?usp=sharing",
        f"https://drive.google.com/open?id={ID}",
        f"https://drive.google.com/uc?id={ID}&export=download",
    ],
)
def test_drive_file_links_become_downloads(link: str) -> None:
    assert public_links.export_url(link) == (f"https://drive.google.com/uc?export=download&id={ID}")


@pytest.mark.parametrize(
    "link",
    [
        "https://docs.google.com/forms/d/e/1FAIpQLSfabc/viewform",
        f"https://docs.google.com/presentation/d/e/2PACX-{ID}/pub",
        "https://drive.google.com/drive/folders/" + ID,
        "https://docs.google.com/",
    ],
)
def test_links_google_cannot_export_are_refused(link: str) -> None:
    with pytest.raises(BotaiError) as exc:
        public_links.export_url(link)
    assert exc.value.code == ErrorCode.VALIDATION


def test_other_links_stay_as_they_are() -> None:
    assert public_links.export_url(" https://files.example.org/a.pdf ") == (
        "https://files.example.org/a.pdf"
    )
    assert not public_links.is_google("https://files.example.org/a.pdf")


def test_only_google_hosts_may_serve_an_export() -> None:
    assert public_links.hop_allowed(f"https://{CONTENT_HOST}/export/x")
    assert public_links.hop_allowed("https://drive.usercontent.google.com/download?id=x")
    for url in (
        "https://evil.example/doc-08-4o-sheets.googleusercontent.com",
        "https://doc-08-4o-sheets.googleusercontent.com.evil.example/x",
        "https://googleusercontent.com/x",
        "http://docs.google.com/x",
        "https://169.254.169.254/latest",
    ):
        assert not public_links.hop_allowed(url), url
    assert public_links.is_sign_in("https://accounts.google.com/v3/signin/identifier")


def adapter(kind: str, url: str) -> DocumentAdapter:
    built = AdapterFactory().create(
        {"adapter": "document", "source": {"kind": kind, "url": url, "refreshEveryMinutes": 720}}
    )
    assert isinstance(built, DocumentAdapter)
    return built


class Google:
    """A stand-in for Google: records every request, answers by host."""

    def __init__(self, *, shared: bool = True, rogue: bool = False) -> None:
        self.shared, self.rogue = shared, rogue
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(f"{request.url.host}{request.url.path}")
        host = request.url.host
        if host == "docs.google.com":
            if not self.shared:
                return httpx.Response(
                    307, headers={"location": "https://accounts.google.com/v3/signin/identifier"}
                )
            where = (
                "https://evil.example/steal"
                if self.rogue
                else (f"https://{CONTENT_HOST}/export/abc?format=xlsx")
            )
            return httpx.Response(307, headers={"location": where})
        if host == CONTENT_HOST:
            return httpx.Response(
                200,
                content=make_xlsx(),
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": (
                        "attachment; filename=\"Exams.xlsx\"; filename*=UTF-8''%D0%AD%D0%BA%D0%B7.xlsx"
                    ),
                },
            )
        return httpx.Response(500)


async def test_a_shared_sheet_is_followed_to_its_file_host_and_named_after_the_document() -> None:
    google = Google()
    ctx = make_ctx(transport=httpx.MockTransport(google), allow_hosts=("docs.google.com",))
    raw = await adapter("google_sheet", SHEET).fetch_bytes(ctx)
    assert not isinstance(raw, EndpointOutcome)
    data, _ = raw
    assert data == make_xlsx()
    assert google.seen == [
        f"docs.google.com/spreadsheets/d/{ID}/export",
        f"{CONTENT_HOST}/export/abc",
    ]
    assert ctx.filename == "Экз.xlsx"  # the document's own name, not "export" or "edit"


async def test_a_sheet_that_is_not_shared_by_link_is_forbidden_not_empty() -> None:
    google = Google(shared=False)
    ctx = make_ctx(transport=httpx.MockTransport(google), allow_hosts=("docs.google.com",))
    outcome = await adapter("google_sheet", SHEET).fetch_bytes(ctx)
    assert isinstance(outcome, EndpointOutcome)
    assert (outcome.ok, outcome.status) == (False, 403)
    assert google.seen == [f"docs.google.com/spreadsheets/d/{ID}/export"]  # sign-in is not visited


async def test_a_redirect_to_a_host_that_is_not_google_is_never_followed() -> None:
    google = Google(rogue=True)
    ctx = make_ctx(transport=httpx.MockTransport(google), allow_hosts=("docs.google.com",))
    outcome = await adapter("google_sheet", SHEET).fetch_bytes(ctx)
    assert isinstance(outcome, EndpointOutcome)
    assert not outcome.ok
    assert all("evil" not in seen for seen in google.seen)


async def test_a_sign_in_page_instead_of_a_file_is_forbidden() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>Sign in</html>", headers={"content-type": "text/html"}
        )

    ctx = make_ctx(transport=httpx.MockTransport(handler), allow_hosts=("drive.google.com",))
    link = f"https://drive.google.com/file/d/{ID}/view"
    outcome = await adapter("url", link).fetch_bytes(ctx)
    assert isinstance(outcome, EndpointOutcome)
    assert outcome.status == 403


async def test_a_missing_document_is_not_found() -> None:
    ctx = make_ctx(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        allow_hosts=("docs.google.com",),
    )
    outcome = await adapter(
        "google_doc", f"https://docs.google.com/document/d/{ID}/edit"
    ).fetch_bytes(ctx)
    assert isinstance(outcome, EndpointOutcome)
    assert outcome.problem == "not_found"

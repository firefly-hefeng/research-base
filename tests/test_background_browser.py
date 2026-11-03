import base64
import json
import hashlib

import pytest

from research_base.browser_worker import BackgroundBrowserWorker
from research_base.models import ResearchError


class Bridge:
    def __init__(self, *, borrowed=False, fail_input=False, raw=None, corrupt_chunk=False):
        self.calls = []
        self.borrowed, self.fail_input = borrowed, fail_input
        self.raw = raw or b"%PDF-1.7\nfixture bytes\n%%EOF"
        self.corrupt_chunk = corrupt_chunk

    def __call__(self, action, args):
        self.calls.append((action, args))
        if action == "attach_tab":
            return {"borrowed": self.borrowed, "tabId": args["tabId"]}
        if action == "navigate":
            return {"tabId": 42}
        if action == "cdp":
            method = args["method"]
            if method == "Page.getLayoutMetrics":
                return {"cssVisualViewport": {"clientWidth": 900, "clientHeight": 600}}
            if method == "Page.captureScreenshot":
                return {"data": "observed-image"}
            if method == "Input.dispatchMouseEvent" and self.fail_input:
                raise RuntimeError("input interrupted")
            return {}
        if action == "evaluate":
            if "document.querySelectorAll" in args["code"]:
                return {"type":"string","value":json.dumps({"url":"https://example.org/paper","title":"Paper","links":[{"url":"https://example.org/paper.pdf","text":"Download PDF"}]})}
            raw = self.raw
            if "delete globalThis" in args["code"]:
                value = {"deleted":True}
            elif "base64:btoa" in args["code"]:
                options = json.loads(args["code"].split("const options=",1)[1].split(";",1)[0])
                chunk = raw[options["offset"]:options["offset"]+options["length"]]
                if self.corrupt_chunk:
                    chunk = b"X" + chunk[1:]
                value = {"offset":options["offset"],"bytes":len(chunk),"base64":base64.b64encode(chunk).decode()}
            else:
                value = {"status":200,"validPdf":True,"url":"https://example.org/paper.pdf","bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
            return {"type":"string","value":json.dumps(value)}
        return {}


def test_main_pdf_selection_skips_html_preview_and_requires_identity_consistency(tmp_path):
    bridge = Bridge()
    worker = BackgroundBrowserWorker(bridge)
    worker.reuse_task_tab(42)
    worker.pdf_links = lambda: {
        "doi": "10.1101/2025.01.01.123456", "links": [
            {"text": "PDF help", "url": "javascript:void(0)"},
            {"text": "PDF help", "url": "mailto:help@example.org"},
            {"text": "Preview PDF", "url": "https://example.org/2025.01.01.123456v2.full.pdf+html"},
            {"text": "Download PDF", "url": "https://example.org/2025.01.01.123456v2.full.pdf"},
            {"text": "citation_pdf_url", "url": "https://example.org/early/2025/2025.01.01.123456.full.pdf"}]}
    selected = worker.select_pdf_link("https://doi.org/10.1101/2025.01.01.123456")
    assert selected["url"].endswith("v2.full.pdf")
    assert selected["requires_identity_validation"] is True
    with pytest.raises(ResearchError) as mismatch:
        worker.select_pdf_link("10.1101/2025.01.02.999999")
    assert mismatch.value.code == "BROWSER_IDENTITY_MISMATCH"
    worker.pdf_links = lambda: {"links": [{"text": "Preview PDF", "url": "https://example.org/paper.pdf+html"}]}
    with pytest.raises(ResearchError) as preview:
        worker.select_pdf_link()
    assert preview.value.code == "BROWSER_PDF_LINK_REQUIRED"
    assert not any(action == "evaluate" for action, _ in bridge.calls)


def test_background_input_never_activates_desktop_and_restores_focus_emulation():
    bridge = Bridge()
    worker = BackgroundBrowserWorker(bridge, background_input=True)
    worker.open("https://example.org/paper")
    view = worker.capture_page()
    worker.click_in_page(view["observation_id"], 70, 120)
    methods = [args["method"] for action, args in bridge.calls if action == "cdp"]
    assert "Page.bringToFront" not in methods and "Target.activateTarget" not in methods
    assert methods.count("Input.dispatchMouseEvent") == 3
    assert bridge.calls[-1] == ("cdp", {"method":"Emulation.setFocusEmulationEnabled","params":{"enabled":False}})
    with pytest.raises(ResearchError, match="current page screenshot"):
        worker.click_in_page(view["observation_id"], 70, 120)


def test_background_input_cleanup_even_if_backend_input_fails():
    bridge = Bridge(fail_input=True)
    worker = BackgroundBrowserWorker(bridge, background_input=True)
    worker.reuse_task_tab(42)
    view = worker.capture_page()
    with pytest.raises(RuntimeError):
        worker.click_in_page(view["observation_id"], 30, 40)
    assert bridge.calls[-1][1] == {"method":"Emulation.setFocusEmulationEnabled","params":{"enabled":False}}


def test_borrowed_researcher_tabs_and_unavailable_input_are_rejected():
    with pytest.raises(ResearchError) as error:
        BackgroundBrowserWorker(Bridge(borrowed=True)).reuse_task_tab(42)
    assert error.value.code == "BROWSER_USER_TAB"
    bridge = Bridge()
    worker = BackgroundBrowserWorker(bridge)
    worker.reuse_task_tab(42)
    with pytest.raises(ResearchError) as error:
        worker.click_in_page("fake", 30, 40)
    assert error.value.code == "BROWSER_CAPABILITY_REQUIRED"
    assert not any(action == "cdp" for action, args in bridge.calls)


def test_only_observed_pdf_links_are_downloaded_and_bytes_still_need_identity_validation(tmp_path):
    worker = BackgroundBrowserWorker(Bridge())
    worker.reuse_task_tab(42)
    with pytest.raises(ResearchError) as error:
        worker.download_pdf("https://other.org/private.pdf", tmp_path)
    assert error.value.code == "BROWSER_LINK_NOT_OBSERVED"
    result = worker.download_pdf("https://example.org/paper.pdf", tmp_path)
    assert result["execution_policy"]["foreground_allowed"] is False
    assert __import__('pathlib').Path(result["path"]).read_bytes().startswith(b"%PDF-")
    assert "identity" in result["notice"] or "validation" in result["notice"]


def test_pdf_transfer_chunks_match_full_browser_digest(tmp_path):
    raw = b"%PDF-1.7\n" + b"original" * 150000 + b"%%EOF"
    worker = BackgroundBrowserWorker(Bridge(raw=raw))
    worker.reuse_task_tab(42)
    result = worker.download_pdf("https://example.org/paper.pdf",tmp_path)
    assert result["transfer_chunks"] == 3
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert __import__('pathlib').Path(result["path"]).read_bytes() == raw


def test_corrupt_or_incomplete_transfer_never_leaves_a_publishable_pdf(tmp_path):
    bridge = Bridge(corrupt_chunk=True)
    worker = BackgroundBrowserWorker(bridge)
    worker.reuse_task_tab(42)
    with pytest.raises(ResearchError):
        worker.download_pdf("https://example.org/paper.pdf",tmp_path)
    assert not list(tmp_path.iterdir())
    assert "delete globalThis" in bridge.calls[-1][1]["code"]

"""Task-owned background browser adapter; never controls the desktop.

The transport is trusted host code. Only task-created tabs are accepted. No
generic CDP/evaluate/OS-input method is exposed to a research model.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .models import ResearchError
from .storage import uid


class WebBridgeTransport:
    def __init__(self, session: str, *, address="http://127.0.0.1:10086", client=None):
        parsed = urlsplit(address)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password:
            raise ResearchError("INVALID_ARGUMENT", "Use a trusted local browser bridge")
        self.session, self.address = session, address.rstrip("/")
        self.client = client or httpx.Client(timeout=90, trust_env=False)

    def __call__(self, action, arguments):
        try:
            response = self.client.post(self.address + "/command", json={"session": self.session, "action": action, "args": arguments})
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise ResearchError("BROWSER_UNAVAILABLE", "The local browser transport failed", retryable=True) from None
        if result.get("ok") is not True:
            raise ResearchError("BROWSER_UNAVAILABLE", "The browser bridge could not complete this operation", retryable=True)
        return result["data"]

    def close(self):
        self.client.close()


class BackgroundBrowserWorker:
    """Background DOM, page screenshots, in-tab input and bounded PDF transfer.

    Background input must be explicitly supported by the backend. It uses the
    browser's tab-targeted input, never the system mouse or keyboard. Website
    challenges may still fail; failure remains visible instead of taking over
    the researcher's desktop. Close browser tabs only on the user's request.
    """
    def __init__(self, transport, *, background_input=False):
        self._transport = transport
        self.background_input = background_input
        self.tab_id = None
        self._observation = None

    @property
    def execution_policy(self):
        return {"mode": "background", "foreground_allowed": False, "os_input_allowed": False,
                "user_tabs_allowed": False, "background_input": self.background_input}

    def _send(self, action, arguments=None):
        return self._transport(action, arguments or {})

    def _bound(self):
        if self.tab_id is None:
            raise ResearchError("BROWSER_TAB_REQUIRED", "Create or select a task-owned browser tab first")
        # Another worker may have selected a different tab in this bridge
        # session. Rebind the owned target without activating a window.
        attached = self._send("attach_tab", {"tabId": self.tab_id})
        if attached.get("borrowed") is not False:
            self.tab_id = None
            raise ResearchError("BROWSER_USER_TAB", "Background workers cannot use a researcher's borrowed tab")

    def open(self, url: str, *, group_title="Research Base"):
        self._url(url)
        if self.tab_id is None:
            result = self._send("navigate", {"url": url, "newTab": True, "group_title": group_title})
            self.tab_id = result["tabId"]
            self._bound()
        else:
            self._bound()
            result = self._send("navigate", {"url": url})
        self._observation = None
        return {"tab_id": self.tab_id, "requested_url": url, "execution_policy": self.execution_policy}

    def reuse_task_tab(self, tab_id: int):
        if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 1:
            raise ResearchError("INVALID_ARGUMENT", "Use a task-owned tab identifier")
        self.tab_id = tab_id
        self._bound()
        self._observation = None

    @staticmethod
    def _url(url):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ResearchError("INVALID_ARGUMENT", "Use an ordinary credential-free literature URL")

    def snapshot(self, *, max_chars=30000):
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 60000:
            raise ResearchError("INVALID_ARGUMENT", "Invalid snapshot budget")
        self._bound()
        return self._send("snapshot", {"format": "aria", "max_chars": max_chars})

    def _evaluate(self, code):
        self._bound()
        result = self._send("evaluate", {"code": code})
        if result.get("type") != "string":
            raise ResearchError("BROWSER_RESPONSE_INVALID", "Expected structured browser data")
        try:
            return json.loads(result["value"])
        except (ValueError, KeyError, TypeError):
            raise ResearchError("BROWSER_RESPONSE_INVALID", "Browser returned invalid structured data") from None

    def pdf_links(self):
        return self._evaluate("(() => JSON.stringify({url:location.href,title:document.title,links:Array.from(document.querySelectorAll('a')).filter(a=>/PDF/i.test(a.textContent)||/\\.pdf(?:[?#]|$)|showPdf|\\/pdf|pdfft/i.test(a.href)).slice(0,200).map(a=>({text:a.textContent.trim().slice(0,300),url:a.href}))}))()")

    def capture_page(self):
        self._bound()
        metrics = self._send("cdp", {"method": "Page.getLayoutMetrics", "params": {}})
        shot = self._send("cdp", {"method": "Page.captureScreenshot", "params": {"format": "png", "captureBeyondViewport": False}})
        viewport = metrics.get("cssVisualViewport", metrics.get("visualViewport", {}))
        self._observation = {"id": uid("browser_view"), "width": viewport.get("clientWidth"), "height": viewport.get("clientHeight")}
        return {"observation_id": self._observation["id"], "image_base64": shot["data"], "mime_type": "image/png",
                "viewport": dict(self._observation), "execution_policy": self.execution_policy}

    def click_in_page(self, observation_id: str, x: float, y: float):
        view = self._observation
        if not self.background_input:
            raise ResearchError("BROWSER_CAPABILITY_REQUIRED", "This backend has not enabled in-tab background input")
        if (not view or observation_id != view["id"] or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in (x, y))
                or not isinstance(view.get("width"), (int, float)) or not isinstance(view.get("height"), (int, float))
                or not 0 <= x < view["width"] or not 0 <= y < view["height"]):
            raise ResearchError("BROWSER_OBSERVATION_REQUIRED", "Use coordinates from the current page screenshot")
        self._bound()
        self._observation = None
        try:
            self._send("cdp", {"method": "Emulation.setFocusEmulationEnabled", "params": {"enabled": True}})
            for event in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {"type": event, "x": x, "y": y}
                if event != "mouseMoved":
                    params.update(button="left", clickCount=1)
                self._send("cdp", {"method": "Input.dispatchMouseEvent", "params": params})
        finally:
            self._send("cdp", {"method": "Emulation.setFocusEmulationEnabled", "params": {"enabled": False}})
        return {"input_sent": True, "execution_policy": self.execution_policy,
                "next_action": "Inspect the page again; input delivery does not prove the site accepted it."}

    def download_pdf(self, url: str, directory: str | Path, *, max_bytes=100 * 1024 * 1024):
        self._url(url)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 100 * 1024 * 1024:
            raise ResearchError("INVALID_ARGUMENT", "Invalid PDF transfer budget")
        observed = self.pdf_links()
        if url not in {item["url"] for item in observed["links"]}:
            raise ResearchError("BROWSER_LINK_NOT_OBSERVED", "Choose a PDF link from the current task page")
        # The controlled expression fetches an observed page link using this
        # browser session. It does not export cookies or change browser security.
        key = uid("research_base_pdf")
        options = json.dumps({"url":url,"key":key,"max":max_bytes})
        code = "(async()=>{const options=" + options + ";" + """
const r=await fetch(options.url,{credentials:'same-origin'});const reader=r.body.getReader();let chunks=[],n=0;
for(;;){const v=await reader.read();if(v.done)break;n+=v.value.length;if(n>options.max){await reader.cancel();return JSON.stringify({status:r.status,tooLarge:true});}chunks.push(v.value);}
const b=new Uint8Array(n);let p=0;for(const c of chunks){b.set(c,p);p+=c.length;}
const valid=new TextDecoder().decode(b.slice(0,8)).startsWith('%PDF-');
if(!r.ok||!valid||!globalThis.crypto?.subtle)return JSON.stringify({status:r.status,url:r.url,validPdf:false,bytes:n});
const digest=await crypto.subtle.digest('SHA-256',b);const sha=Array.from(new Uint8Array(digest)).map(v=>v.toString(16).padStart(2,'0')).join('');
globalThis[options.key]=b;return JSON.stringify({status:r.status,url:r.url,validPdf:true,bytes:n,sha256:sha});})()"""
        part = None
        try:
            downloaded = self._evaluate(code)
            size = downloaded.get("bytes")
            if downloaded.get("status") != 200 or downloaded.get("validPdf") is not True:
                raise ResearchError("BROWSER_FULLTEXT_UNAVAILABLE", "The observed link did not return a complete PDF", retryable=True,
                                    details={"http_status": downloaded.get("status"), "too_large": downloaded.get("tooLarge", False)})
            if (not isinstance(size,int) or isinstance(size,bool) or not 5 <= size <= max_bytes
                    or not isinstance(downloaded.get("sha256"),str) or not re.fullmatch(r"[a-f0-9]{64}", downloaded["sha256"])):
                raise ResearchError("BROWSER_RESPONSE_INVALID", "Invalid browser download manifest")
            target_dir = Path(directory).expanduser().resolve()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / (uid("browser_pdf") + ".pdf")
            part = target.with_suffix(".part")
            digest = hashlib.sha256()
            with part.open("wb") as stream:
                for offset in range(0,size,512*1024):
                    length = min(512*1024,size-offset)
                    config = json.dumps({"key":key,"offset":offset,"length":length})
                    expression = "(()=>{const options="+config+";"+"""const b=globalThis[options.key];if(!(b instanceof Uint8Array))return JSON.stringify({missing:true});const c=b.subarray(options.offset,options.offset+options.length);let s='';for(let i=0;i<c.length;i+=32768)s+=String.fromCharCode(...c.subarray(i,i+32768));return JSON.stringify({offset:options.offset,bytes:c.length,base64:btoa(s)});})()"""
                    chunk = self._evaluate(expression)
                    try:
                        raw = base64.b64decode(chunk["base64"],validate=True)
                    except (ValueError,KeyError,TypeError):
                        raise ResearchError("BROWSER_RESPONSE_INVALID", "Invalid PDF transfer chunk") from None
                    if (chunk.get("offset") != offset or len(raw) != length or chunk.get("bytes") != length
                            or (offset == 0 and not raw.startswith(b"%PDF-"))):
                        raise ResearchError("INVALID_FILE", "Incomplete or invalid PDF transfer chunk")
                    digest.update(raw)
                    stream.write(raw)
            if digest.hexdigest() != downloaded["sha256"]:
                raise ResearchError("INVALID_FILE", "Browser and local PDF checksums differ")
            part.replace(target)
            return {"path":str(target),"source_url":downloaded["url"],"bytes":size,"sha256":digest.hexdigest(),
                    "transfer_chunks":(size+512*1024-1)//(512*1024),"execution_policy":self.execution_policy,
                    "notice":"Downloaded bytes still require the library's paper/version/format validation before availability is claimed."}
        finally:
            if part is not None:
                part.unlink(missing_ok=True)
            try:
                self._evaluate("(()=>{delete globalThis["+json.dumps(key)+"];return JSON.stringify({deleted:true});})()")
            except ResearchError:
                pass  # Navigating away also releases the old page's bounded cache.

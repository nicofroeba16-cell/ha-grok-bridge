from __future__ import annotations

import copy
import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from auto_control_center.refresh import canonical_event_id, stable_payload_signature
from auto_control_center.simulation import build_simulation

HTML = (ROOT / "auto_control_center/static/index.html").read_text(encoding="utf-8")
EVIDENCE = ROOT / "auto_control_center/evidence/control-center-ha-integration-final-v3"
VIDEOS = EVIDENCE / "Videos"
SHOTS = EVIDENCE / "Screenshots"
ACCEPTANCE = EVIDENCE / "Acceptance"
for path in (VIDEOS, SHOTS, ACCEPTANCE):
    path.mkdir(parents=True, exist_ok=True)

initial = build_simulation(worker_count=32, event_count=80, wake_count=48)
changed = copy.deepcopy(initial)
worker = changed["workers"][0]
worker["state"] = "WAITING_FOR_USER"
worker["resolved_state"] = "WAITING_FOR_USER"
worker["user_gate"] = ["Fixture-only approval gate"]
worker["activation_source"] = None
worker["activation_confirmed"] = None
worker["checkpoint_action"] = "REPORT_GATE"
worker["checkpoint_resume"] = False
changed["stats"]["running"] = max(0, int(changed["stats"]["running"]) - 1)
changed["stats"]["waiting_for_user"] = int(changed["stats"]["waiting_for_user"]) + 1
changed["stats"]["blocked"] = int(changed["stats"]["blocked"]) + 1
changed["refresh"]["signature"] = stable_payload_signature(changed)
changed["refresh"]["event_id"] = canonical_event_id(changed)

degraded = copy.deepcopy(changed)
for source in ("orchestrator_db", "browser_wake_db", "browser_routes"):
    degraded["health"][source] = {"present": False, "readable": False}
degraded["wake_path"]["healthy"] = False
degraded["wake_path"]["components_active"] = 0
degraded["wake_path"]["unhealthy_count"] = 3
degraded["refresh"]["signature"] = stable_payload_signature(degraded)
degraded["refresh"]["event_id"] = canonical_event_id(degraded)

state = {"payload": initial, "fail": False, "dashboard_requests": 0}
metrics = {"viewports": {}, "interaction": {}, "console_errors": [], "page_errors": [], "reduced_motion": {}}

FAKE_EVENT_SOURCE = r"""
window.__accSources=[];
window.__autoOpen=true;
class FakeEventSource {
  static OPEN=1; static CONNECTING=0; static CLOSED=2;
  constructor(url){
    this.url=url; this.readyState=FakeEventSource.CONNECTING; this.listeners={};
    window.__accSources.push(this);
    if(window.__autoOpen) setTimeout(()=>this.open(),0);
  }
  addEventListener(name,cb){(this.listeners[name]??=[]).push(cb)}
  open(){if(this.readyState===FakeEventSource.CLOSED)return;this.readyState=FakeEventSource.OPEN;if(this.onopen)this.onopen({})}
  emit(name,payload){for(const cb of (this.listeners[name]||[]))cb({data:JSON.stringify(payload)})}
  fail(){if(this.readyState===FakeEventSource.CLOSED)return;this.readyState=FakeEventSource.CONNECTING;if(this.onerror)this.onerror({})}
  close(){this.readyState=FakeEventSource.CLOSED}
}
window.EventSource=FakeEventSource;
"""


def route_handler(route):
    url = route.request.url
    if url.rstrip("/") == "http://127.0.0.1:8877":
        route.fulfill(status=200, content_type="text/html", body=HTML)
    elif "/api/dashboard" in url:
        state["dashboard_requests"] += 1
        if state["fail"]:
            route.fulfill(status=200, content_type="application/json", body="not-json")
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(state["payload"]))
    elif "/api/actions/wake-all/preview" in url:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "enabled": False, "available": False, "can_submit": False,
            "eligible": [], "skipped": [], "eligible_count": 0,
            "skipped_count": 0, "routed_count": 0,
        }))
    else:
        route.fulfill(status=404, body="not found")


def save_video(page, target: Path) -> None:
    source = Path(page.video.path())
    shutil.copyfile(source, target)


def run_viewport(browser, name: str, width: int, height: int, *, mobile: bool) -> None:
    temp_video = EVIDENCE / f".video-{name}"
    temp_video.mkdir(exist_ok=True)
    context = browser.new_context(
        viewport={"width": width, "height": height},
        record_video_dir=str(temp_video),
        record_video_size={"width": width, "height": height},
        reduced_motion="reduce" if mobile else "no-preference",
    )
    page = context.new_page()
    page.on("console", lambda msg: metrics["console_errors"].append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: metrics["page_errors"].append(str(exc)))
    page.add_init_script(FAKE_EVENT_SOURCE)
    page.route("**/*", route_handler)
    state.update(payload=initial, fail=False)
    page.goto("http://127.0.0.1:8877/", wait_until="domcontentloaded")
    page.wait_for_function("document.getElementById('statWorkers').textContent==='32'")
    page.wait_for_timeout(350)

    layout = page.evaluate("""()=>({
      viewport:innerWidth,
      body:document.body.scrollWidth,
      nav:document.querySelector('.section-nav').scrollWidth,
      navClient:document.querySelector('.section-nav').clientWidth,
      graph:document.querySelector('.graph-scroll').scrollWidth,
      sources:window.__accSources.length,
      reduced:getComputedStyle(document.documentElement).scrollBehavior,
    })""")
    metrics["viewports"][name] = layout
    assert layout["body"] == layout["viewport"], layout
    assert layout["nav"] <= layout["navClient"], layout
    assert layout["sources"] == 1

    screenshot = SHOTS / f"control-center-v3-{name}-overview-{width}x{height}.png"
    page.screenshot(path=str(screenshot))

    # Provenance: isolate the unconfirmed fixture worker and expose its evidence.
    page.locator("#workerSearch").fill("Runner Docs Freshness E2E")
    page.wait_for_timeout(300)
    assert "Aktivierung unbestätigt" in page.locator("#workers").inner_text()
    page.locator("#workers details").first.evaluate("el=>el.open=true")
    page.wait_for_timeout(450)
    page.screenshot(path=str(SHOTS / f"control-center-v3-{name}-provenance-{width}x{height}.png"))
    page.locator("#workerSearch").fill("")

    # Dependency and media views.
    page.locator("#graph").scroll_into_view_if_needed()
    page.wait_for_timeout(350)
    assert page.locator("#graph svg").count() == 1
    page.locator("#mediaArchive").scroll_into_view_if_needed()
    page.wait_for_timeout(450)
    media_text = page.locator("#mediaArchive").inner_text()
    for value in ("PENDING", "RUNNING", "BLOCKED", "VERIFIED"):
        assert value in media_text
    page.screenshot(path=str(SHOTS / f"control-center-v3-{name}-media-{width}x{height}.png"))

    # Idempotent identical events must not repaint worker nodes.
    page.evaluate("window.__firstWorker=document.querySelector('.worker')")
    for _ in range(20):
        page.evaluate("window.__accSources.at(-1).emit('heartbeat',{})")
    for _ in range(50):
        page.evaluate("(p)=>window.__accSources.at(-1).emit('dashboard',p)", initial)
    metrics["interaction"][f"{name}_node_preserved"] = page.evaluate("window.__firstWorker===document.querySelector('.worker')")
    assert metrics["interaction"][f"{name}_node_preserved"]

    # Material state transition.
    start = time.monotonic()
    page.evaluate("(p)=>window.__accSources.at(-1).emit('dashboard',p)", changed)
    page.wait_for_function("document.querySelector('[data-worker-state=\"WAITING_FOR_USER\"]')!==null")
    metrics["interaction"][f"{name}_material_change_ms"] = round((time.monotonic() - start) * 1000, 1)
    page.wait_for_timeout(400)

    # Reconnect and stale are explicit and never presented as live truth.
    page.evaluate("window.__autoOpen=false")
    active_before = page.evaluate("window.__accSources.length")
    page.evaluate("window.__accSources.at(-1).fail()")
    page.wait_for_function("document.getElementById('connection').textContent.includes('Reconnecting')")
    page.wait_for_timeout(350)
    page.evaluate("setConnection('stale',lastData)")
    page.wait_for_function("document.getElementById('connection').textContent.includes('Stale')")
    assert "Last known" in page.locator("body").evaluate("el=>getComputedStyle(document.querySelector('.state-RUNNING'),'::before').content")
    page.screenshot(path=str(SHOTS / f"control-center-v3-{name}-stale-{width}x{height}.png"))
    page.wait_for_timeout(450)

    page.evaluate("window.__autoOpen=true")
    page.evaluate("connectStream(); window.__accSources.at(-1).open()")
    page.wait_for_function("document.getElementById('connection').textContent.includes('Live')")
    page.evaluate("(p)=>window.__accSources.at(-1).emit('dashboard',p)", degraded)
    page.wait_for_function("document.getElementById('connection').textContent.includes('Degraded')")
    page.wait_for_timeout(350)
    page.evaluate("(p)=>window.__accSources.at(-1).emit('dashboard',p)", initial)
    page.wait_for_function("document.getElementById('connection').textContent.includes('Live')")
    metrics["interaction"][f"{name}_sources_created"] = page.evaluate("window.__accSources.length") - active_before
    metrics["interaction"][f"{name}_active_sources"] = page.evaluate("window.__accSources.filter(s=>s.readyState!==EventSource.CLOSED).length")
    assert metrics["interaction"][f"{name}_active_sources"] <= 1

    if mobile:
        metrics["reduced_motion"][name] = page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert metrics["reduced_motion"][name] is True
        assert "prefers-reduced-transparency:reduce" in HTML

    page.wait_for_timeout(500)
    video_obj = page.video
    context.close()
    source = Path(video_obj.path())
    target = VIDEOS / f"control-center-v3-{name}-{width}x{height}.webm"
    shutil.copyfile(source, target)
    shutil.rmtree(temp_video, ignore_errors=True)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    run_viewport(browser, "desktop", 1440, 1100, mobile=False)
    run_viewport(browser, "iphone", 393, 852, mobile=True)
    browser.close()

assert not metrics["console_errors"], metrics["console_errors"]
assert not metrics["page_errors"], metrics["page_errors"]
for key, value in metrics["interaction"].items():
    if key.endswith("material_change_ms"):
        assert value < 2000

(ACCEPTANCE / "visual-metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(metrics, indent=2, ensure_ascii=False))

"""Start and stop the whole stack: two fakes, the services process and the gateway.

Order matters. The fake Stripe needs the gateway's address to deliver webhooks, so the
gateway port is chosen before anything starts; the services process needs the fake OpenAI;
the gateway needs both fakes and the address the services process reports in its READY line.

`down()` stops the gateway first and gracefully, because that is what stops the engine
workers it started, then the services process, then the fakes, and finally sweeps any engine
that outlived the gateway.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import env as environment
from .paths import (
    REPO_ROOT,
    RUN_ROOT,
    SERVER_DIR,
    add_server_to_path,
    ensure_dir,
    run_dir,
    venv_python,
)
from .ports import free_port, port_open
from .procs import Proc, ProcessError, sweep_engines
from .state import HarnessState, clear_state, read_state, write_state

log = logging.getLogger("e2e.harness")

READY_TIMEOUT_S = 120.0
GATEWAY_READY_TIMEOUT_S = 180.0
# Both spellings the gateway logs: the dev spawner's "ready" line and the pool's "started" line.
ENGINE_PID_RE = re.compile(r"engine (\d+) (?:ready|started) \(pid (\d+)\)")


@dataclass(frozen=True)
class RealAI:
    """The REAL AI provider instead of the stand-in: only for `python -m e2e.eval --real`.

    The journeys and every automated run use the stand-in and never set this. The key comes from
    the person running the evaluation; the run's daily and monthly AI budgets are set to
    `budget_usd`, so the product's own budget guard stops it at that spend, and the world service
    reads the real web (news feeds, linked pages) instead of staying offline.
    """

    api_key: str
    budget_usd: str
    base_url: str = "https://api.openai.com/v1"


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class Stack:
    def __init__(
        self,
        run_id: str | None = None,
        state_file: Path | None = None,
        *,
        gateway_port: int | None = None,
        extension_id: str | None = None,
        real_ai: RealAI | None = None,
        plans_file: Path | None = None,
        fake_ai_env: dict[str, str] | None = None,
    ) -> None:
        # A browser run pins both: the extension is built against one origin and Chrome derives
        # its id from the unpacked key. An API run leaves them unset (free port, fixed fake id).
        self.fixed_gateway_port = environment.gateway_port(gateway_port)
        self.extension_id = environment.extension_id(extension_id)
        self.run_id = run_id or new_run_id()
        self.dir = run_dir(self.run_id)
        self.log_dir = self.dir / "logs"
        self.data_dir = self.dir / "data"
        self.state_file = Path(state_file) if state_file else RUN_ROOT / "state.json"
        self.procs: dict[str, Proc] = {}
        self.env: dict[str, str] = {}
        self.gateway_url = ""
        self.services_url = ""
        self.fake_openai_url = ""
        self.fake_google_url = ""
        self.fake_stripe_url = ""
        self.fake_stripe_pages = ""
        self.services_port = 0
        self.stripe_env: dict[str, str] = {}
        self.state: HarnessState | None = None
        self._python = venv_python()
        self.real_ai = real_ai
        self.plans_file = Path(plans_file) if plans_file else None
        # Extra settings for the stand-in AI provider only, e.g. E2E_AI_SCRIPT (e2e/eval --golden).
        self.fake_ai_env = dict(fake_ai_env or {})

    # ------------------------------------------------------------------ helpers

    @property
    def control_db(self) -> Path:
        return self.data_dir / "control" / "control.db"

    def _spawn(self, name: str, command: list[str], cwd: Path, extra_env: dict[str, str]) -> Proc:
        proc = Proc(
            name,
            command,
            cwd=cwd,
            env=environment.process_environment(extra_env),
            log_dir=self.log_dir,
        )
        self.procs[name] = proc
        return proc

    @staticmethod
    def _http_ok(url: str, timeout: float = 3.0) -> bool:
        try:
            response = httpx.get(url, timeout=timeout, trust_env=False)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # ------------------------------------------------------------------ up

    def up(self) -> HarnessState:
        ensure_dir(self.log_dir)
        ensure_dir(self.data_dir)
        try:
            return self._up()
        except BaseException:
            self.down(keep_run_dir=True)
            raise

    def _up(self) -> HarnessState:
        gateway_port = self.fixed_gateway_port or free_port()
        if self.fixed_gateway_port and port_open(self.fixed_gateway_port):
            raise ProcessError(
                f"port {self.fixed_gateway_port} is already in use; stop the other harness first "
                f"(python -m e2e.harness down)"
            )
        self.gateway_url = f"http://127.0.0.1:{gateway_port}"
        ai_key = environment.fake_ai_key()
        stripe_key = environment.fake_stripe_key()
        webhook_secret = environment.fake_webhook_secret()
        secret_key = environment.b64key()
        services_key = environment.b64key()

        openai_port, stripe_port, stripe_tls_port = free_port(), free_port(), free_port()
        fake_openai = None if self.real_ai is not None else self._spawn(
            "fake-openai",
            [self._python, "-m", "e2e.harness.serve_fake", "openai"],
            REPO_ROOT,
            {"E2E_PORT": str(openai_port), "E2E_AI_API_KEY": ai_key, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
             **self.fake_ai_env},
        )
        fake_stripe = self._spawn(
            "fake-stripe",
            [self._python, "-m", "e2e.harness.serve_fake", "stripe"],
            REPO_ROOT,
            {
                "E2E_PORT": str(stripe_port),
                "E2E_TLS_PORT": str(stripe_tls_port),
                "E2E_TLS_DIR": str(self.dir / "tls"),
                "E2E_STRIPE_API_KEY": stripe_key,
                "E2E_STRIPE_WEBHOOK_SECRET": webhook_secret,
                "E2E_GATEWAY_URL": self.gateway_url,
                "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1",
            },
        )
        google_secret = environment.fake_google_secret()
        fake_google = self._spawn(
            "fake-google",
            [self._python, "-m", "e2e.harness.serve_fake", "google"],
            REPO_ROOT,
            {
                "E2E_PORT": str(free_port()),
                "E2E_GOOGLE_CLIENT_ID": environment.GOOGLE_CLIENT_ID,
                "E2E_GOOGLE_CLIENT_SECRET": google_secret,
                "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1",
            },
        )
        if fake_openai is not None:
            self.fake_openai_url = fake_openai.wait_ready_line(READY_TIMEOUT_S)["listen"]
        stripe_ready = fake_stripe.wait_ready_line(READY_TIMEOUT_S)
        self.fake_stripe_url = stripe_ready["listen"]
        self.fake_stripe_pages = stripe_ready.get("pages", self.fake_stripe_url)
        if fake_openai is not None:
            fake_openai.wait_until(lambda: self._http_ok(f"{self.fake_openai_url}/_control/health"), 30, "the fake AI provider")
        self.fake_google_url = fake_google.wait_ready_line(READY_TIMEOUT_S)["listen"]
        fake_google.wait_until(lambda: self._http_ok(f"{self.fake_google_url}/_control/health"), 30, "the fake Google sign-in")
        fake_stripe.wait_until(lambda: self._http_ok(f"{self.fake_stripe_url}/_control/health"), 30, "the fake Stripe API")

        self.env = environment.base_environment(
            data_dir=self.data_dir,
            public_url=self.gateway_url,
            secret_key=secret_key,
            services_token_key=services_key,
            ai_base_url=f"{self.fake_openai_url}/v1" if self.real_ai is None else self.real_ai.base_url,
            ai_api_key=ai_key if self.real_ai is None else self.real_ai.api_key,
            stripe_api_base=self.fake_stripe_url,
            stripe_secret_key=stripe_key,
            stripe_webhook_secret=webhook_secret,
            extension_ids=self.extension_id,
        )
        if self.real_ai is not None:
            # The run can spend no more than this: the product's own budget guard stops it.
            self.env["AI_DAILY_BUDGET_USD"] = self.real_ai.budget_usd
            self.env["AI_MONTHLY_BUDGET_USD"] = self.real_ai.budget_usd
            self.env["STARLING_WORLD_OFFLINE"] = "false"
        if self.plans_file is not None:
            self.env["PLANS_FILE"] = str(self.plans_file)
        self.stripe_env = self._stripe_setup()
        self.env.update(self.stripe_env)
        # A real browser applies the site's `form-action` policy to the redirect that follows the
        # billing form. Production names Stripe's hosted domains; here the hosted pages are the
        # fake's, so the gateway is told their origin (loopback https only, refused in production).
        if self.fake_stripe_pages.startswith("https://"):
            self.env["STARLING_E2E_PAYMENT_PAGES_ORIGIN"] = self.fake_stripe_pages.rstrip("/")
        # Continue with Google, against the stand-in (loopback only; production refuses the variable).
        self.env["GOOGLE_CLIENT_ID"] = environment.GOOGLE_CLIENT_ID
        self.env["GOOGLE_CLIENT_SECRET"] = google_secret
        self.env["STARLING_E2E_OIDC_BASE"] = self.fake_google_url

        self._start_services()
        self._start_gateway(gateway_port)

        state = HarnessState(
            run_id=self.run_id,
            gateway_url=self.gateway_url,
            services_url=self.services_url,
            fake_openai_url=f"{self.fake_openai_url}/v1" if self.real_ai is None else "",
            fake_openai_control=f"{self.fake_openai_url}/_control" if self.real_ai is None else "",
            fake_stripe_url=self.fake_stripe_url,
            fake_stripe_control=f"{self.fake_stripe_url}/_control",
            fake_google_url=self.fake_google_url,
            extension_id=self.extension_id,
            owner_email=environment.OWNER_EMAIL,
            run_dir=str(self.dir),
            log_dir=str(self.log_dir),
            data_dir=str(self.data_dir),
            control_db=str(self.control_db),
            state_file=str(self.state_file),
            pids={name: proc.pid for name, proc in self.procs.items()},
            settings={
                "consent_version": environment.CONSENT_VERSION,
                "extension_min_version": environment.EXTENSION_MIN_VERSION,
                "extension_version": environment.EXTENSION_VERSION,
                "support_email": environment.SUPPORT_EMAIL,
                "daily_budget_usd": environment.DAILY_BUDGET_USD,
                "monthly_budget_usd": environment.MONTHLY_BUDGET_USD,
                "plans": self._plans(),
                "ai_models": {"quality": "gpt-5.6-luna", "fast": "gpt-5.6-luna", "vision": "gpt-5.6-luna"},
            },
            stripe={
                **self.stripe_env,
                "pages_url": self.fake_stripe_pages,
                "checkout_base": f"{self.fake_stripe_pages}/c/pay",
                "portal_base": f"{self.fake_stripe_pages}/p/session",
                "tls": "self-signed; clients do not verify it",
            },
        )
        write_state(self.state_file, state)
        self.state = state
        log.info("harness up: %s", self.gateway_url)
        return state

    def _plans(self) -> dict[str, Any]:
        add_server_to_path()
        from starling_server.config import load_plans

        plans = load_plans(Path(self.env["PLANS_FILE"]))
        return {
            "unit": {"name": plans.unit_name, "label": plans.unit_label},
            "codes": {
                plan.code: {
                    "name": plan.name,
                    "monthly_credits": plan.monthly_credits,
                    "amount_cents": plan.amount_cents,
                    "cost_ceiling_micros": plan.cost_ceiling_micros,
                    "rate_per_minute": plan.rate_per_minute,
                    "rate_per_day": plan.rate_per_day,
                }
                for plan in plans.by_code.values()
            },
        }

    def _stripe_setup(self) -> dict[str, str]:
        """Create the prices and both portal configurations in the fake, as the operator would."""
        add_server_to_path()
        from starling_server.billing import setup
        from starling_server.billing.stripe_client import StripeClient
        from starling_server.config import load_settings
        from starling_server.db import Database

        settings = load_settings(self.env)
        db = Database(settings.control_db_path)
        try:
            with StripeClient(settings) as client:
                report = setup.run_setup(db, settings, client)
        finally:
            db.close()
        if report.problems:
            raise ProcessError("stripe-setup failed:\n- " + "\n- ".join(report.problems))
        (self.log_dir / "stripe-setup.txt").write_text(
            "\n".join([*(f"created  {line}" for line in report.created),
                       *(f"existing {line}" for line in report.reused),
                       *(f"warning  {line}" for line in report.warnings),
                       json.dumps(report.env, indent=2, sort_keys=True)]) + "\n",
            encoding="utf-8",
        )
        return dict(report.env)

    def _start_services(self, overrides: dict[str, str] | None = None) -> None:
        extra = dict(self.env)
        extra.update(overrides or {})
        # The port is pinned for the life of the run: engines are given
        # `STARLING_SERVICES_URL` when they are spawned, so a restart with different settings
        # (journey 5 raises and lowers the AI budget) has to come back on the same address.
        if not self.services_port:
            self.services_port = free_port()
        extra["STARLING_SERVICES_LISTEN"] = f"tcp:127.0.0.1:{self.services_port}"
        proc = self._spawn("services", [self._python, "-m", "starling_server.services.main"], SERVER_DIR, extra)
        ready = proc.wait_ready_line(READY_TIMEOUT_S)
        listen = str(ready["listen"])
        if not listen.startswith("tcp:"):
            raise ProcessError(f"services reported an unexpected listen address: {listen}")
        self.services_url = "http://" + listen[len("tcp:"):]
        proc.wait_until(lambda: self._http_ok(f"{self.services_url}/livez"), 30, "the services process")
        self.env["STARLING_SERVICES_URL"] = self.services_url

    def _start_gateway(self, port: int) -> None:
        # `--log-config` is what makes the gateway's own loggers (the engine pool, billing, the
        # usage sweeper) reach the run folder; uvicorn's `--log-level` only sets its own.
        command = [
            self._python, "-m", "uvicorn",
            "starling_server.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
            "--no-access-log", "--log-config", str(Path(__file__).with_name("logging.json")),
        ]
        proc = self._spawn("gateway", command, SERVER_DIR, self.env)
        proc.wait_until(self._gateway_ready, GATEWAY_READY_TIMEOUT_S, "the gateway /readyz")

    def _gateway_ready(self) -> bool:
        response = httpx.get(f"{self.gateway_url}/readyz", timeout=5.0, trust_env=False)
        if response.status_code == 200:
            return True
        log.debug("gateway not ready yet: %s", response.text[:200])
        return False

    # ------------------------------------------------------------------ operations a journey needs

    def restart_services(self, overrides: dict[str, str] | None = None) -> str:
        """Stop the services process and start it again, optionally with different settings.

        Journey 5 uses it twice: once with a tiny AI budget so the next call trips the daily
        stop, and once with the budget raised again, which is what clears the stop
        (CONTRACTS 4.4: the process clears budget stops below the configured limits at startup).
        """
        proc = self.procs.pop("services", None)
        if proc is not None:
            proc.stop(grace_s=15)
        self._start_services(overrides)
        if self.state is not None:
            self.state.services_url = self.services_url
            self.state.pids["services"] = self.procs["services"].pid
            write_state(self.state_file, self.state)
        # The gateway holds no services connection state, but engines cache one per URL.
        return self.services_url

    def restart_gateway(self, overrides: dict[str, str] | None = None) -> str:
        """Stop the gateway and start it again on the same port, optionally with new settings.

        Part 2's "outdated extension" journey raises `STARLING_EXTENSION_MIN_VERSION` above the
        installed build's version and restarts, which is exactly how the operator rolls out a
        required update. The port is kept because the extension compiles one origin in.

        Engines are the gateway's children; stopping it gracefully stops them, and the next
        request starts a fresh one. Overrides persist in `self.env`, so pass the original value
        back to undo one.
        """
        port = int(self.gateway_url.rsplit(":", 1)[1])
        proc = self.procs.pop("gateway", None)
        engines = self.engine_pids()
        if proc is not None:
            proc.stop(grace_s=30)
        sweep_engines(engines)
        self.env.update(overrides or {})
        self._start_gateway(port)
        if self.state is not None:
            self.state.pids["gateway"] = self.procs["gateway"].pid
            write_state(self.state_file, self.state)
        return self.gateway_url

    def crash_gateway(self) -> str:
        """Kill the gateway the hard way and start it again on the same port.

        No signal it can handle, no shutdown hooks, nothing released on the way down: what a crash,
        an out-of-memory kill or a pulled plug leaves behind. Journey 9 starts a draft first, so the
        control database is left with a live reservation that no process will ever finish.
        """
        port = int(self.gateway_url.rsplit(":", 1)[1])
        proc = self.procs.pop("gateway", None)
        engines = self.engine_pids()
        if proc is not None:
            proc.kill()
            proc.stop(grace_s=5)  # already dead: this only closes the pipes
        sweep_engines(engines)
        self._start_gateway(port)
        if self.state is not None:
            self.state.pids["gateway"] = self.procs["gateway"].pid
            write_state(self.state_file, self.state)
        return self.gateway_url

    def engine_pids(self) -> set[int]:
        pids: set[int] = set()
        gateway = self.procs.get("gateway")
        if gateway is not None:
            for path in (self.log_dir / "gateway.out.txt", self.log_dir / "gateway.err.txt"):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                pids.update(int(match.group(2)) for match in ENGINE_PID_RE.finditer(text))
        try:
            conn = sqlite3.connect(f"file:{self.control_db}?mode=ro", uri=True, timeout=5)
            try:
                pids.update(int(row[0]) for row in conn.execute("SELECT pid FROM engines WHERE pid IS NOT NULL"))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        return {pid for pid in pids if pid > 0}

    # ------------------------------------------------------------------ down

    def down(self, keep_run_dir: bool = True) -> dict[str, Any]:
        pids = self.engine_pids()
        report: dict[str, Any] = {"stopped": {}, "engines_swept": []}
        for name in ("gateway", "services", "fake-stripe", "fake-openai"):
            proc = self.procs.pop(name, None)
            if proc is None:
                continue
            grace = 30.0 if name == "gateway" else 10.0
            report["stopped"][name] = proc.stop(grace_s=grace)
        for proc in list(self.procs.values()):
            proc.stop(grace_s=10.0)
        self.procs.clear()
        # Engines are children of the gateway. A graceful gateway stops them; this catches the
        # case where it was killed instead.
        report["engines_swept"] = sweep_engines(pids)
        clear_state(self.state_file)
        if not keep_run_dir:
            report["run_dir"] = str(self.dir)
        return report

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "gateway": self.gateway_url,
            "processes": {
                name: {"pid": proc.pid, "alive": proc.alive()} for name, proc in self.procs.items()
            },
        }


def attach(state_file: Path) -> HarnessState | None:
    """The state of a harness another process started, when it is still answering."""
    state = read_state(state_file)
    if state is None:
        return None
    try:
        response = httpx.get(f"{state.gateway_url}/healthz", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return None
    return state if response.status_code == 200 else None


def stop_from_state(state_file: Path) -> dict[str, Any]:
    """Stop a stack started by another process, using only what the state file records."""
    state = read_state(state_file)
    if state is None:
        return {"ok": False, "reason": "no state file"}
    report: dict[str, Any] = {"signalled": [], "engines_swept": []}
    from .procs import alive, kill_pid, process_command_line

    order = ["gateway", "services", "fake-stripe", "fake-openai"]
    engine_pids: set[int] = set()
    try:
        conn = sqlite3.connect(f"file:{state.control_db}?mode=ro", uri=True, timeout=5)
        try:
            engine_pids = {int(row[0]) for row in conn.execute("SELECT pid FROM engines WHERE pid IS NOT NULL")}
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    for name in order:
        pid = int(state.pids.get(name, 0) or 0)
        if pid <= 0 or not alive(pid):
            continue
        command = process_command_line(pid) or ""
        if "python" not in command.lower() and "uvicorn" not in command.lower():
            continue
        kill_pid(pid)
        report["signalled"].append(name)
        if name == "gateway":
            time.sleep(1.0)
    report["engines_swept"] = sweep_engines(engine_pids)
    clear_state(Path(state.state_file))
    return report


__all__ = ["Stack", "attach", "stop_from_state", "new_run_id"]

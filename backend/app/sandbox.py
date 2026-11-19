"""E2B sandbox wrapper: run untrusted / model-written Python in an isolated cloud VM."""

from dataclasses import dataclass

from app.config import get_settings

MAX_OUTPUT = 8000


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    result: str = ""
    error: str = ""

    def as_text(self) -> str:
        if not self.ok:
            return f"执行失败: {self.error}"
        parts = [p for p in (self.stdout.strip(), self.result.strip()) if p]
        return ("\n".join(parts) or "(无输出)")[:MAX_OUTPUT]


async def run_code(code: str, timeout: float = 60) -> SandboxResult:
    api_key = get_settings().e2b_api_key
    if not api_key:
        return SandboxResult(ok=False, error="沙箱未配置（缺少 E2B_API_KEY）")
    from e2b_code_interpreter import AsyncSandbox

    sandbox = None
    try:
        sandbox = await AsyncSandbox.create(api_key=api_key, timeout=int(timeout) + 60)
        execution = await sandbox.run_code(code, timeout=timeout)
        if execution.error:
            return SandboxResult(ok=False, error=f"{execution.error.name}: {execution.error.value}")
        return SandboxResult(
            ok=True,
            stdout="".join(execution.logs.stdout),
            result=execution.text or "",
        )
    except Exception as exc:
        return SandboxResult(ok=False, error=str(exc)[:500])
    finally:
        if sandbox is not None:
            await sandbox.kill()

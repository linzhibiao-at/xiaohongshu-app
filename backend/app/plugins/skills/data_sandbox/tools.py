from app.sandbox import run_code


async def run_python(code: str) -> str:
    """在隔离的 E2B 沙箱里执行 Python 代码并返回输出。

    Args:
        code: 要执行的 Python 源码，用 print() 输出结果。
    """
    result = await run_code(code)
    return result.as_text()


TOOLS = [run_python]

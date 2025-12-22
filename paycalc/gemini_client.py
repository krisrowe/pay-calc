
"""Gemini CLI client for processing files and prompts with AI."""

import subprocess
import json
import tempfile
import pathlib
import shutil
import re
from typing import Optional


# Standard wrapper for JSON extraction prompts
PROMPT_PREFIX = """You are a data extraction assistant. Your task is to analyze the provided file and return structured JSON data.

CRITICAL INSTRUCTIONS:
1. Return ONLY valid JSON - no markdown code blocks, no explanations, no commentary
2. Your entire response must be parseable by a JSON parser
3. All numeric values must be numbers (not strings)
4. All dates must be in YYYY-MM-DD format
5. If you cannot extract the requested data, return an error object:
   {"error": true, "message": "description of what went wrong", "details": "any helpful context"}

FILE TO ANALYZE: {file_path}

EXTRACTION TASK:
"""

PROMPT_SUFFIX = """

Remember: Return ONLY the JSON object. No other text."""


def _run_gemini_cli(
    prompt: str,
    timeout: int = 120,
    cwd: Optional[str] = None,
) -> str:
    """
    Run Gemini CLI with a prompt and return raw output.

    This is the core subprocess runner shared by process_file and process_prompt.

    Args:
        prompt: The prompt to send to Gemini.
        timeout: Timeout in seconds (default 120).
        cwd: Working directory for the subprocess (optional).

    Returns:
        The raw stdout from Gemini CLI.

    Raises:
        RuntimeError: If Gemini CLI fails or times out.
    """
    cmd = [
        'gemini',
        '--allowed-mcp-server-names', 'none',
        '-o', 'text',
        prompt
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            cwd=cwd
        )
        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Gemini CLI timed out after {timeout}s")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Gemini CLI failed with exit code {e.returncode}.\n"
            f"Stderr: {e.stderr}"
        ) from e


def process_prompt(
    prompt: str,
    timeout: int = 120,
) -> str:
    """
    Process a simple text prompt using Gemini CLI.

    Unlike process_file, this does not wrap the prompt or expect JSON output.
    It returns the raw text response from Gemini.

    Args:
        prompt: The prompt to send to Gemini.
        timeout: Timeout in seconds (default 120).

    Returns:
        The text response from Gemini.

    Raises:
        RuntimeError: If Gemini CLI fails or times out.
    """
    return _run_gemini_cli(prompt, timeout=timeout)


def get_stock_quote(ticker: str, timeout: int = 30) -> float:
    """
    Get the last closing price for a stock ticker.

    Uses Gemini to look up the most recent closing price.

    Args:
        ticker: Stock ticker symbol (e.g., "SBUX", "JPM").
        timeout: Timeout in seconds (default 30).

    Returns:
        The last closing price as a float.

    Raises:
        RuntimeError: If Gemini CLI fails or times out.
        ValueError: If the response cannot be parsed as a number.
    """
    prompt = (
        f"Tell me at what price did the stock {ticker.upper()} last close as a "
        f"decimal point number with no dollar sign and no other text so that "
        f"your response can be parsed as a decimal number"
    )

    response = _run_gemini_cli(prompt, timeout=timeout)

    try:
        return float(response.strip())
    except ValueError:
        raise ValueError(
            f"Could not parse stock quote response as number: '{response}'"
        )


def process_file(
    prompt: str,
    data_file_path: str,
    timeout: int = 120
) -> dict:
    """
    Process a data file using Gemini CLI with a prompt.

    Copies the file to a temp directory, invokes Gemini CLI with file access,
    and returns the parsed JSON response. The prompt is automatically wrapped
    with instructions for reliable JSON output.

    Args:
        prompt: The extraction task description (what data to extract and schema).
               Use {file_path} placeholder if you need to reference the file.
        data_file_path: Path to the data file (e.g., a PDF) to process.
        timeout: Timeout in seconds for Gemini CLI (default 120).

    Returns:
        A dictionary containing the parsed JSON response from Gemini.
        If extraction fails, returns {"error": true, "message": "...", "details": "..."}.

    Raises:
        RuntimeError: If Gemini CLI fails or file not found.
    """
    data_path = pathlib.Path(data_file_path)
    if not data_path.exists():
        raise RuntimeError(f"File not found: {data_file_path}")

    # Create temp directory and copy file with sanitized name
    temp_dir = tempfile.mkdtemp(prefix="gemini_")
    temp_dir_path = pathlib.Path(temp_dir)

    # Sanitize filename: replace spaces/special chars, keep extension
    import re
    safe_stem = re.sub(r'[^a-zA-Z0-9_-]', '_', data_path.stem)
    safe_name = f"{safe_stem}{data_path.suffix}"
    temp_data_file = temp_dir_path / safe_name

    try:
        shutil.copy(data_file_path, temp_data_file)

        # Build prompt with sanitized filename (we'll run gemini from temp dir)
        wrapped_prompt = PROMPT_PREFIX + prompt + PROMPT_SUFFIX
        full_prompt = wrapped_prompt.replace("{file_path}", safe_name)

        # Run Gemini from temp directory so file is in its workspace
        response_str = _run_gemini_cli(full_prompt, timeout=timeout, cwd=temp_dir)

        # Handle markdown code blocks in response
        if '```json' in response_str:
            response_str = response_str.split('```json')[1].split('```')[0].strip()
        elif '```' in response_str:
            response_str = response_str.split('```')[1].split('```')[0].strip()

        # Try to find JSON object if not at start
        if not response_str.strip().startswith('{'):
            start = response_str.find('{')
            end = response_str.rfind('}') + 1
            if start >= 0 and end > start:
                response_str = response_str[start:end]

        return json.loads(response_str)

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse JSON from Gemini output: {e}\n"
            f"Raw output: {response_str[:500] if 'response_str' in dir() else 'N/A'}..."
        ) from e
    finally:
        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python gemini-client.py <prompt> <file_path>")
        print("  Process a file with Gemini using the given prompt.")
        print("  Use {file_path} in prompt to reference the file.")
        sys.exit(1)

    prompt = sys.argv[1]
    file_path = sys.argv[2]

    try:
        result = process_file(prompt, file_path)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

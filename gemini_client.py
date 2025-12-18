
"""Gemini CLI client for processing files with AI extraction."""

import subprocess
import json
import tempfile
import pathlib
import shutil
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
        cmd = [
            'gemini',
            '--allowed-mcp-server-names', 'none',
            '-o', 'text',
            full_prompt
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            cwd=temp_dir  # Run from temp dir so file is accessible
        )

        response_str = result.stdout.strip()

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

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Gemini CLI timed out after {timeout}s")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Gemini CLI failed with exit code {e.returncode}.\n"
            f"Stderr: {e.stderr}"
        ) from e
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

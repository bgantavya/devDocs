from argparse import ArgumentParser, Namespace
from logging import INFO, WARNING, basicConfig, exception, getLogger, info
from os import chdir, curdir, getcwd, listdir, makedirs, scandir
from os.path import abspath, dirname, exists, getsize, isdir, join, splitext
from typing import List, Optional, Set

from google.genai import Client
from google.genai.types import GenerateContentConfig

basicConfig(level=INFO)
getLogger("google_genai").setLevel(WARNING)
getLogger("httpx").setLevel(WARNING)

# Runtime globals initialized in main().
client: Optional[Client] = None
start_dir: str = ""
DOCS_BASE: str = "docs"
data: str = ""
ow: bool = False

prompt = '''
You are Gantavya Bansal, a senior software engineer and expert technical writer. Your task is to generate clean, professional, and well-structured `README.md` documentation in Markdown format. Use the provided filename, source code, and any existing README or folder structure as context.

Your output must be:

- Concise and easy to follow
- Focused on technical clarity and usability
- Markdown-only (no extra commentary, no code fences)

Your output must include:

1. **Project Title** – Inferred from the filename or main script
2. **Folder Structure** – Tree view if available, with clickable index links
3. **Description** – What the project does and its purpose
4. **How to Use** – Installation steps, CLI/API usage examples
5. **Technologies Used** – Languages, tools, libraries
6. **Architecture or Code Overview** – Key components, flow, functions, or classes
7. **Known Issues / Improvements** – Current limitations, TODOs
8. **Additional Notes or References** – Licensing, credits, related tools

Only return the final `README.md` content. Do not include any explanations, prefixes, or suffixes.

                    '''

# Directory names skipped wholesale. Matched against the whole path segment,
# never as a substring. The previous list was matched with `kw in name`, which
# meant 'out' removed routes.py/layout.tsx/checkout.tsx, 'lib' removed
# library.ts, 'bin' removed binary_search.py and 'env' removed environment.py.
# Measured against CPython's stdlib, that silently dropped 432 of 1446 files.
# Cheapest capable model first. The list was previously ordered the other way
# round, so every successful call paid the top rate for a job the lite models
# handle fine. Escalation now only happens when the cheaper model fails.
MODELS: List[str] = [
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-3.1-pro-preview",
]

avoid: Set[str] = {
    'node_modules', '__pycache__', 'dist', 'build', 'coverage', 'vendor',
    'venv', '.venv', 'env', 'target', 'bin', 'obj', 'out', 'logs',
    '.next', '.turbo', '.expo', '.idea', '.vscode', '.git', 'site-packages',
    'migrations', 'fixtures', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    'eggs', 'wheels', 'htmlcov', '.tox', '.eggs', 'third_party',
}

# Whole filenames with no documentable content. Exact match, case-insensitive.
skip_files: Set[str] = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock',
    'uv.lock', 'cargo.lock', 'composer.lock', 'gemfile.lock',
    '.ds_store', 'thumbs.db', '.gitkeep',
}

# Leading characters that mark a dotfile or editor artefact. '_' is deliberately
# absent: it was removing every __init__.py, which is the file that defines a
# Python package's public surface.
prefix: List[str] = ['.', '~']
suffix: List[str] = [
    '.log', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.gif', '.webp',
    '.pyc', '.class', '.zip', '.min.js', '.mp4', '.mp3', '.wav', '.pdf',
    '.docx', '.xlsx', '.db', '.sqlite', '.bak', '.7z', '.rar', '.tar.gz',
    '.exe', '.dll', '.so', '.ttf', '.woff', '.eot', '.swp', '.map', '.webm',
    '.md', '.css'
]

# Names the caller explicitly forced back in via --include.
forced: Set[str] = set()

# Every path this run declined to read, so the user can see what was dropped.
skipped: List[str] = []


# HTTP statuses where trying another model cannot help.
_FATAL = frozenset({400, 401, 403})

# Models this run has proven unusable, so we stop paying to rediscover that.
dead_models: Set[str] = set()


def _status_of(err: Exception) -> Optional[int]:
    """Best-effort HTTP status from an SDK exception."""
    for attr in ('code', 'status_code'):
        value = getattr(err, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(err, 'response', None)
    value = getattr(response, 'status_code', None)
    return value if isinstance(value, int) else None


def _text_of(response: object) -> Optional[str]:
    """Extract markdown from a response, or None when it carried no content.

    `response.text` is None whenever finishReason is SAFETY or MAX_TOKENS,
    because the candidate has no parts at all.
    """
    text = getattr(response, 'text', None)
    if not text:
        return None
    return text.removeprefix('```markdown').removesuffix('```').strip() or None


def GenerateReadMe(file: str, code: str, readme: str) -> str:
    """Generate markdown docs using Gemini with fallback model sequence."""
    if client is None:
        return f"# {file}\n\n⚠️ Failed to generate documentation. API client is not initialized."

    last_error: Optional[Exception] = None

    for model_name in MODELS:
        if model_name in dead_models:
            continue
        try:
            response = client.models.generate_content(
                model=model_name,
                config=GenerateContentConfig(system_instruction=prompt),
                contents=[
                    f"Filename: {file}",
                    f"Code:\n{code}",
                    f"Existing README (if any):\n{readme}",
                ],
            )
        except Exception as err:
            last_error = err
            status = _status_of(err)
            if status in _FATAL:
                # Bad key or disabled API: every other model fails identically,
                # so escalating burns 7 more requests to learn nothing.
                exception(f"{status} from Gemini; aborting. Check your API key.")
                raise SystemExit(2)
            if status == 404:
                # Model does not exist for this key. Never retry it this run.
                dead_models.add(model_name)
            continue

        text = _text_of(response)
        if text is not None:
            return text
        # A 200 with no parts (SAFETY / MAX_TOKENS) is not a transport failure;
        # a larger model will not fix it.
        info(f"{model_name} returned no content for {file}")
        break

    exception(f"Error generating README for {file}: {last_error}")
    return f"# {file}\n\nDocumentation could not be generated for this file."


def print_tree(start_path: str = '.', prefix: str = '') -> str:
    """Build a filtered tree-like text representation for the given directory."""
    try:
        tree = ''
        files: List[str] = []
        folders: List[str] = []

        if not isdir(start_path):
            parent = dirname(abspath(start_path))
            if isdir(parent):
                start_path = parent
            else:
                return ""

        with scandir(start_path) as entries:
            for entry in entries:
                if IsValid(entry.name, entry.is_dir()):
                    if entry.is_dir():
                        folders.append(entry.name)
                    else:
                        files.append(entry.name)

        folders.sort()
        files.sort()
        ordered_entries = folders + files

        for index, name in enumerate(ordered_entries):
            path = join(start_path, name)
            is_last = index == len(ordered_entries) - 1

            branch = '└── ' if is_last else '├── '
            tree += prefix + branch + name + '\n'

            if name in folders:
                extension = '    ' if is_last else '│   '
                tree += print_tree(path, prefix + extension)

        return tree
    except Exception as err:
        exception(f"Error generating Tree for {start_path} dir: {err}")
        return f"# {start_path}\n\n⚠️ Failed to generate documentation tree."


def FolderReadMe(base: str, folders: List[str], files: List[str]) -> None:
    """Generate a folder-level README summary from nested files and folders."""
    try:
        all_content = LookReadMe(base)
        all_content += f"\n {print_tree(start_path=base)} \n"

        for folder in folders:
            path = join(getcwd(), folder)
            all_content += f"\n readme for folder:{folder} \n content inside: \n {LookReadMe(path)} \n"

        for file in files:
            all_content += f"\n readme for file:{file} \n content inside: {LookReadMe(file)} \n"

        root_name = 'README' if base == '.' else base
        WriteReadMe(root_name, all_content, LookReadMe(root_name))
        info("")
    except Exception as err:
        exception(f"Error generating README for {base}: {err}")


def LookReadMe(file: str) -> str:
    """Read an adjacent markdown file if available."""
    try:
        md_path = file + '.md'
        if exists(md_path):
            with open(md_path, 'r', encoding='utf-8') as readme:
                return readme.read()
        return ""
    except Exception as err:
        exception(f"Error reading README for {file}: {err}")
        return f"# {file}\n\n⚠️ Failed to read {file}.md"


def LookCode(file: str) -> str:
    """Read source code content from a file path."""
    try:
        with open(file, 'r', encoding='utf-8') as code_file:
            return code_file.read()
    except Exception as err:
        exception(f"Error reading code in {file}: {err}")
        return f"# {file}\n\n⚠️ Failed to read {file}"


def WriteReadMe(file: str, code: str, readme: str) -> None:
    """Create markdown output for one file and write it to docs or root README."""
    try:
        rel_path = getcwd().replace(start_dir, '').lstrip('\\/').replace('\\', '/')
        docs_dir = join(start_dir, DOCS_BASE, rel_path)
        makedirs(docs_dir, exist_ok=True)

        filename = splitext(file)[0] + '.md'
        if 'README' in filename.upper():
            if not ow:
                info("skipping overwriting README")
                full_path = join(docs_dir, 'README.md')
            else:
                full_path = join('README.md')
        else:
            full_path = join(docs_dir, filename)

        generated_prompt_input = data + readme
        with open(full_path, 'w', encoding='utf-8') as readme_file:
            readme_file.write(GenerateReadMe(file, code, generated_prompt_input))

        print(f"Written to: {filename}")
    except Exception as err:
        exception(f"Error writing README for {file}: {err}")


def LookFolder(base: str) -> None:
    """Recursively scan folders and generate markdown docs for valid files."""
    try:
        chdir(base)
        print(f"Reading Folder: {base}")

        entries: List[str] = [
            entry for entry in listdir()
            if IsValid(entry, isdir(join(getcwd(), entry)))
        ]
        folders: List[str] = [
            folder for folder in entries
            if isdir(join(getcwd(), folder))
        ]

        if folders:
            print("Folders found:")
            for folder in folders:
                info(folder)

            for folder in folders:
                info("")
                print(f"Opening Folder: {folder}")
                LookFolder(folder)
                print(f"Closing Folder: {folder}")
                info("")

        files: List[str] = [
            file for file in entries
            if (not isdir(join(getcwd(), file))) and (getsize(file) < 1_000_000)
        ]

        if files:
            print("Files found:")
            for file in files:
                info(file)

            for file in files:
                code = LookCode(file)
                readme = LookReadMe(file)
                WriteReadMe(file, code, readme)

        FolderReadMe(base, folders, files)
        chdir('..')
    except Exception:
        exception(f"Failed to read {base} folder.")


def Look(include: str, exclude: str) -> None:
    """Apply the user's include/exclude filters.

    These were inverted: `--include foo` appended foo to the *skip* list and
    `--exclude foo` removed it, so both flags did the opposite of their help
    text. They now mean what they say.
    """
    try:
        for item in (i.strip().lower() for i in include.split(',')):
            if item:
                forced.add(item)
                avoid.discard(item)
                skip_files.discard(item)
                if item in suffix:
                    suffix.remove(item)

        for item in (e.strip().lower() for e in exclude.split(',')):
            if item:
                forced.discard(item)
                if item.startswith('.'):
                    if item not in suffix:
                        suffix.append(item)
                else:
                    avoid.add(item)
    except Exception:
        exception("Error in use with args --include || --exclude")


def IsValid(entry: str, is_dir: bool = False) -> bool:
    """Return True when a file/folder name passes the configured filters.

    Matching is by whole path segment, not substring. `--include` forces a name
    back in and wins over every other rule.
    """
    name = entry.lower()

    if name in forced:
        return True

    if any(name.startswith(sw) for sw in prefix):
        skipped.append(entry)
        return False

    if is_dir:
        if name in avoid:
            skipped.append(entry + '/')
            return False
        return True

    if name in skip_files:
        skipped.append(entry)
        return False

    if any(name.endswith(ew) for ew in suffix):
        skipped.append(entry)
        return False

    # A file sharing a name with a skipped directory (e.g. `build.py`, `out.go`)
    # is still source and is kept; only the bare name is rejected.
    if name in avoid:
        skipped.append(entry)
        return False

    return True


def main() -> None:
    """CLI entrypoint for docs generation workflow."""
    try:
        parser = ArgumentParser(
            description="Auto-generate documentation from source code and folder structure."
        )
        parser.add_argument(
            "-p", "--path",
            type=str,
            default=".",
            help="Root path to scan (default: current directory)"
        )
        parser.add_argument(
            "--name",
            type=str,
            default="My Project",
            help="Project name to include in README"
        )
        parser.add_argument(
            "--description",
            type=str,
            default="No description provided.",
            help="Short description of the project"
        )
        parser.add_argument(
            "--authors",
            type=str,
            default="Anonymous",
            help="Comma-separated list of author names"
        )
        parser.add_argument(
            "--keywords",
            type=str,
            default="",
            help="Comma-separated keywords (e.g., cli, docs, auto)"
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing README files (default: False)"
        )
        parser.add_argument(
            "--output",
            type=str,
            default='docs',
            help="Output dir where docs to be stored (default: docs)"
        )
        parser.add_argument(
            "--exclude",
            type=str,
            default='',
            help="Folders, files, extensionse to exclude ((e.g., docs, ext, setting, config)"
        )
        parser.add_argument(
            "--include",
            type=str,
            default='',
            help="Folders, files, extensionse to include ((e.g., docs, ext, setting, config)"
        )

        global client
        global start_dir
        global DOCS_BASE
        global data
        global ow

        args: Namespace = parser.parse_args()
        start_dir = getcwd()

        ow = args.overwrite
        DOCS_BASE = args.output
        Look(include=args.include, exclude=args.exclude)

        if not exists(DOCS_BASE):
            makedirs(DOCS_BASE)

        avoid.add(DOCS_BASE.lower())
        data = (
            f"name: {args.name}\n"
            f"description: {args.description}\n"
            f"authors: {args.authors}\n"
            f"keywords: {args.keywords}"
        )

        client = Client(api_key=input("Paste your Google Gemini API Key here:").strip())
        print(f"📁 Starting in: {args.path}")
        makedirs(DOCS_BASE, exist_ok=True)
        chdir(args.path)
        LookFolder(curdir)
        print("Documentation generated successfully.")
        if skipped:
            print(f"Skipped {len(skipped)} path(s). Use --include to force any back in.")
    except Exception:
        exception("Error during execution. Try using --help.")


if __name__ == "__main__":
    main()

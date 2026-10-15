"""Check relative links in the repository's Markdown files.

Reports Markdown links whose relative target file or directory does not exist, and
#anchors that match no heading in the target Markdown file (GitHub slug rules).
External URLs and links outside the repository are not fetched or checked.

    python3 host/check_links.py          # exit 1 if any broken link is found
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', '.claude', 'build', '__pycache__'}
LINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)|!\[[^\]]*\]\(([^)\s]+)\)')
FENCE = re.compile(r'^\s*(```|~~~)')


def markdown_files():
    for path in sorted(ROOT.rglob('*.md')):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith('build') for part in rel.parts[:-1]):
            continue
        yield path


def slug(heading):
    text = re.sub(r'`|\*\*|__|\[([^\]]*)\]\([^)]*\)', r'\1', heading.strip().lower())
    text = re.sub(r'[^\w\- ]', '', text)
    return text.replace(' ', '-')


def anchors(path):
    found, counts, fenced = set(), {}, False
    for line in path.read_text(encoding='utf-8').splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := re.match(r'^#{1,6}\s+(.*?)\s*#*\s*$', line)):
            base = slug(m.group(1))
            n = counts.get(base, 0)
            counts[base] = n + 1
            found.add(base if n == 0 else f'{base}-{n}')
    return found


def links(path):
    fenced = False
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r'`[^`]*`', '', line)
        for m in LINK.finditer(line):
            yield number, m.group(1) or m.group(2)


def problems():
    cache = {}
    for path in markdown_files():
        for number, target in links(path):
            if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I):
                continue
            name, _, fragment = target.partition('#')
            dest = (path.parent / name).resolve() if name else path
            where = f'{path.relative_to(ROOT)}:{number}'
            try:
                dest.relative_to(ROOT)
            except ValueError:
                continue                          # sibling repositories are not checked
            if not dest.exists():
                yield f'{where}: missing {target}'
            elif fragment and dest.suffix == '.md':
                if dest not in cache:
                    cache[dest] = anchors(dest)
                if fragment.lower() not in cache[dest]:
                    yield f'{where}: no heading #{fragment} in {dest.relative_to(ROOT)}'


def main():
    found = list(problems())
    print('\n'.join(found) if found else 'all relative Markdown links resolve')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())

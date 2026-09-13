"""Literal supplied attachment names, derived without a new name registry."""
import re

_LABEL = re.compile(r"\b(?:reference|filename|file name|title|label)\s*:\s*([^\n]{3,192})", re.IGNORECASE)
_QUOTED = re.compile(r'\b(?:named|called|titled|labeled|labelled)\s+["“]([^"”\n]{3,160})["”]', re.IGNORECASE)
_FILENAME = re.compile(r'(?<![\w./\\-])([\w][\w.-]{2,120}\.(?:png|jpe?g|webp))(?![\w/\\-]|\.\w)', re.IGNORECASE)


def matching_names(query, text):
    """Exact supplied labels, quoted names and image filenames, not keywords.

    Deliberately abstains on an unnamed subject or a semantic paraphrase. Text
    only supplies a name; the caller must independently find its admitted link.
    """
    query = ' '.join(query.casefold().split())
    names = [match.group(1) for match in _QUOTED.finditer(text)]
    names.extend(match.group(1) for match in _FILENAME.finditer(text))
    for match in _LABEL.finditer(text):
        value = re.split(r'[.!?](?:\s|$|\\[nr])|\\[nr]', match.group(1), maxsplit=1)[0]
        names.append(value.strip(' \"“”'))
    matched = []
    for name in names:
        name = ' '.join(name.strip().split())
        folded = name.casefold()
        if (3 <= len(name) <= 160 and sum(c.isalnum() for c in name) >= 3
                and '/' not in name and '\\' not in name
                and re.search(r'(?<![\w./\\-])' + re.escape(folded) + r'(?![\w/\\-]|\.\w)', query)
                and folded not in {item.casefold() for item in matched}):
            matched.append(name)
    return matched[:8]

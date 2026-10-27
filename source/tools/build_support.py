"""Build-only filtering for this Windows 10+ Qt application."""


def filter_system_icu(binaries):
    """Qt imports the unversioned ICU API provided by Windows System32.

    Poppler/Conda can put a different ABI in a file also named icuuc.dll on
    PATH. Freezing that library shadows the system API and breaks Qt imports.
    Keep versioned private ICU libraries; they have different loader names.
    """
    result = []
    for row in binaries:
        name = row[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
        source = row[1].replace('/', '\\').lower()
        if name == 'icuuc.dll':
            continue
        if name.startswith('icudt') and '\\native\\poppler\\' in source:
            continue
        result.append(row)
    return result

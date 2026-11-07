def slugify(text):
    """Lowercase text and join words with dashes."""
    return "-".join(text.lower().split())

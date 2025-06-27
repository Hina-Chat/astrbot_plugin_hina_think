import hashlib

def _simplify_user_key(user_key: str) -> str:
    """Generate a short, safe, and unique key from the user's ID for use in filenames and paths."""
    # Use SHA-256 to generate a hash of the user key
    hash_object = hashlib.sha256(user_key.encode())
    # Take the first 16 characters of the hex digest for a reasonable balance of brevity and uniqueness
    return hash_object.hexdigest()[:16]

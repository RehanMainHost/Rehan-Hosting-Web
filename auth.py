import hashlib, hmac, base64, os, time
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from Crypto.Cipher import AES
from config import settings

_ser = URLSafeTimedSerializer(settings.SECRET_KEY, salt="rwh-session")

def _aes_key() -> bytes:
    return hashlib.sha256(settings.SECRET_KEY.encode()).digest()

def hash_password(pw: str) -> str:
    return hashlib.sha256((pw + settings.SECRET_KEY).encode()).hexdigest()

def verify_password(pw: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(pw), hashed or "")

def make_session(username: str) -> str:
    return _ser.dumps({"u": username, "t": int(time.time())})

def read_session(token: str):
    try:
        data = _ser.loads(token, max_age=settings.SESSION_MAX_AGE)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None

def encrypt(plain: str) -> str:
    if not plain:
        return ""
    nonce = os.urandom(12)
    cipher = AES.new(_aes_key(), AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plain.encode())
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()

def decrypt(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    try:
        raw = base64.urlsafe_b64decode(cipher_text.encode())
        nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
        cipher = AES.new(_aes_key(), AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag).decode()
    except Exception:
        return ""

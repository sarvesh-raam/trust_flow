from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
from auth import create_access_token, create_refresh_token, verify_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

class FirebaseTokenRequest(BaseModel):
    firebase_token: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 1800

@router.post("/google", response_model=TokenResponse)
async def google_login(req: FirebaseTokenRequest):
    try:
        import jwt
        # Decode the Firebase ID Token. 
        # (For hackathon speed, we decode without signature verification. 
        # For prod, use firebase-admin or fetch Google's public x509 certs).
        
        # Log token preview for debugging
        token_preview = req.firebase_token[:10] + "..." if len(req.firebase_token) > 10 else "SHORT_TOKEN"
        print(f"Attempting login with token preview: {token_preview}")

        unverified_claims = jwt.decode(req.firebase_token, options={"verify_signature": False})
        
        user_id = unverified_claims.get("sub")
        email = unverified_claims.get("email", "")
        
        if not user_id:
            print("Login failed: Could not extract user ID")
            raise HTTPException(401, "Could not extract user ID")
            
        print(f"Login success: {email} ({user_id})")
        return TokenResponse(
            access_token=create_access_token(user_id, email),
            refresh_token=create_refresh_token(user_id)
        )
    except Exception as e:
        print(f"Token verification failed: {type(e).__name__}: {str(e)}")
        raise HTTPException(401, f"Invalid Firebase token: {str(e)}")

@router.post("/refresh")
async def refresh_token(refresh_token: str):
    payload = verify_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(401, "Invalid refresh token")
    
    new_access = create_access_token(payload["sub"], "")
    return {"access_token": new_access, "token_type": "bearer"}

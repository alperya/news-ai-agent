"""
Instagram Access Token Manager
Handles token refresh using long-lived tokens to prevent expiration
Reads credentials directly from .env file
"""

import os
import logging
import requests
from datetime import datetime
from typing import Optional, Dict


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InstagramTokenManager:
    """Manage Instagram access tokens with automatic refresh"""
    
    def __init__(self):
        self.graph_api_url = "https://graph.facebook.com/v24.0"
        
    def get_valid_token(self) -> str:
        """
        Get a valid Instagram access token, refreshing if necessary
        
        Strategy:
        1. Load token from .env
        2. Validate token with API
        3. If expired, try to refresh using client_id and client_secret from .env
        4. If refresh fails, return the current token anyway
        5. Return valid token
        
        Returns:
            str: Valid access token
        """
        # Load token from environment
        current_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
        
        if not current_token:
            raise ValueError(
                "INSTAGRAM_ACCESS_TOKEN not found in .env file"
            )
        
        logger.info("📝 Checking Instagram token validity...")
        
        # Validate current token
        token_info = self._get_token_info(current_token)
        if not token_info:
            logger.error("❌ Token validation failed with API")
            logger.info("🔄 Attempting to refresh token...")
            
            # Try to refresh the invalid token
            refreshed_token = self._refresh_token(current_token)
            if refreshed_token:
                logger.info("✅ Token refreshed successfully!")
                return refreshed_token
            else:
                # Refresh failed, but return current token anyway
                logger.warning("⚠️  Using current token despite validation failure")
                return current_token
        
        # Check if token is expired
        if self._is_token_expired(token_info):
            logger.warning("⚠️  Token has EXPIRED")
            logger.info("🔄 Attempting to refresh expired token...")
            
            # Try to refresh the expired token
            refreshed_token = self._refresh_token(current_token)
            if refreshed_token:
                logger.info("✅ Token refreshed successfully!")
                return refreshed_token
            else:
                # Refresh failed, but return current token anyway for fallback
                logger.warning("⚠️  Could not refresh, using current token")
                return current_token
        
        # Token is valid
        hours_remaining = self._get_hours_until_expiry(token_info)
        logger.info(f"✅ Token is valid (expires in {hours_remaining} hours)")
        
        # Check if token is short-lived (less than 7 days = 168 hours)
        # Refresh if less than 1 week remaining to prevent expiration
        if hours_remaining < 168:
            logger.warning(f"⚠️  Token is SHORT-LIVED (only {hours_remaining} hours remaining)")
            logger.info("🔄 Converting to long-lived token...")
            
            refreshed_token = self._refresh_token(current_token)
            if refreshed_token:
                logger.info("✅ Successfully converted to long-lived token!")
                return refreshed_token
            else:
                # Conversion failed
                logger.error("⚠️  ⚠️  Could not convert token to long-lived")
                return current_token
        
        logger.info(f"✅ Token is LONG-LIVED - All good!")
        return current_token
    
    def _get_token_info(self, token: str) -> Optional[Dict]:
        """
        Get token information including expiry time
        
        Args:
            token: Instagram access token
            
        Returns:
            Dict with token info or None if invalid
        """
        try:
            # Get account ID from environment - needed to validate token
            account_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
            if not account_id:
                logger.warning("⚠️  INSTAGRAM_ACCOUNT_ID not set - cannot validate token")
                return None
            
            # Try to get account info to validate token
            url = f"{self.graph_api_url}/{account_id}"
            params = {'access_token': token, 'fields': 'id,name,username'}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            username = data.get('username', data.get('name', 'Unknown'))
            logger.info(f"✅ Token is valid for account: {username}")
            
            # Also check token expiry info if available
            expiry_url = f"{self.graph_api_url}/debug_token"
            expiry_params = {
                'input_token': token,
                'access_token': token
            }
            
            try:
                expiry_response = requests.get(expiry_url, params=expiry_params, timeout=10)
                if expiry_response.status_code == 200:
                    expiry_data = expiry_response.json().get('data', {})
                    expires_at = expiry_data.get('expires_at', 0)
                    if expires_at:
                        expiry_date = datetime.fromtimestamp(expires_at)
                        logger.info(f"   Token expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}")
                        return {
                            'token': token,
                            'created_at': datetime.now().isoformat(),
                            'user_id': data.get('id'),
                            'user_name': username,
                            'expires_at': expires_at
                        }
            except Exception as e:
                logger.debug(f"Could not get token expiry info: {str(e)}")
            
            return {
                'token': token,
                'created_at': datetime.now().isoformat(),
                'user_id': data.get('id'),
                'user_name': username,
                'expires_at': None
            }
            
        except Exception as e:
            logger.warning(f"❌ Token validation failed: {str(e)}")
            return None
    
    def _refresh_token(self, current_token: str) -> Optional[str]:
        """
        Attempt to refresh the access token using long-lived token exchange
        
        For Instagram/Facebook Graph API, we exchange a short-lived token for a long-lived one.
        This requires client_id and client_secret from your app.
        
        Args:
            current_token: Current access token
            
        Returns:
            New access token if refresh successful, None otherwise
        """
        try:
            # Get app credentials for token refresh
            client_id = os.getenv('INSTAGRAM_CLIENT_ID')
            client_secret = os.getenv('INSTAGRAM_CLIENT_SECRET')
            
            if not client_id or not client_secret:
                logger.debug("⚠️  INSTAGRAM_CLIENT_ID or INSTAGRAM_CLIENT_SECRET not set - skipping refresh")
                return None
            
            logger.info("🔄 Exchanging token for long-lived version...")
            
            url = f"{self.graph_api_url}/oauth/access_token"
            params = {
                'grant_type': 'fb_exchange_token',
                'client_id': client_id,
                'client_secret': client_secret,
                'fb_exchange_token': current_token
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            new_token = data.get('access_token')
            
            if not new_token:
                logger.error("❌ No access token in refresh response")
                logger.error(f"Response: {data}")
                return None
            
            logger.info(f"✅ Token refreshed successfully!")
            expires_in = data.get('expires_in', 'Unknown')
            logger.info(f"   Expires in: {expires_in} seconds (~60 days)")
            
            # Get token info to verify refresh
            token_info = self._get_token_info(new_token)
            if not token_info:
                logger.warning("⚠️  Could not verify refreshed token")
            
            # Update environment variable for current session
            os.environ['INSTAGRAM_ACCESS_TOKEN'] = new_token
            logger.info(f"✅ Environment variable updated with new token")
            
            return new_token
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Token refresh failed: {str(e)}")
            if hasattr(e.response, 'text'):
                try:
                    error_data = e.response.json().get('error', {})
                    error_msg = error_data.get('message', e.response.text)
                    logger.error(f"   Error: {error_msg}")
                except Exception:
                    logger.error(f"   Response: {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"❌ Token refresh error: {str(e)}")
            return None
    
    def _is_token_expired(self, token_info: Dict) -> bool:
        """
        Check if token is actually expired (by checking with API)
        
        Args:
            token_info: Token info dict with expiry info
            
        Returns:
            True if token is expired
        """
        try:
            expires_at = token_info.get('expires_at')
            
            if not expires_at:
                # No expiry info available - assume valid
                return False
            
            expiry_date = datetime.fromtimestamp(expires_at)
            time_left = expiry_date - datetime.now()
            
            # Token is expired if time_left is negative
            is_expired = time_left.total_seconds() <= 0
            
            if is_expired:
                logger.warning(f"⚠️  Token expired {abs(time_left)} ago")
            
            return is_expired
            
        except Exception as e:
            logger.debug(f"Token expiry check failed: {str(e)}")
            return False
    
    def _get_hours_until_expiry(self, token_info: Dict) -> int:
        """Get estimated hours until token expires"""
        try:
            expires_at = token_info.get('expires_at')
            
            if not expires_at:
                return 0
            
            expiry_date = datetime.fromtimestamp(expires_at)
            time_left = expiry_date - datetime.now()
            hours_left = int(time_left.total_seconds() / 3600)
            
            return max(0, hours_left)
            
        except Exception:
            return 0


# Convenience function for easy access
def get_instagram_token() -> str:
    """
    Get a valid Instagram access token, refreshing if necessary
    
    Returns:
        str: Valid access token
    """
    manager = InstagramTokenManager()
    return manager.get_valid_token()


if __name__ == "__main__":
    # Test token manager
    manager = InstagramTokenManager()
    try:
        token = manager.get_valid_token()
        logger.info(f"✅ Got valid token: {token[:20]}...{token[-10:]}")
    except Exception as e:
        logger.error(f"❌ Failed to get token: {str(e)}")

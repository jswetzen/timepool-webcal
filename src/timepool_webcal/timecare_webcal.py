"""
TimeCare Pool Schedule Scraper with Webcal Feed
Self-hosted solution to scrape work schedule and serve as webcal
"""
import asyncio
import secrets
import os
import hmac
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from icalendar import Calendar, Event
from bs4 import BeautifulSoup
import uvicorn

# Configuration
TIMECARE_URL = "https://timepool.boras.se"
USERNAME = os.getenv("TIMECARE_USERNAME", "your_username")
PASSWORD = os.getenv("TIMECARE_PASSWORD", "your_password")

# Generate deterministic token from credentials using HMAC
SALT = "timepool-webcal-secret-2024"
if USERNAME != "your_username" and PASSWORD != "your_password":
    CALENDAR_TOKEN = hmac.new(
        SALT.encode(),
        f"{USERNAME}:{PASSWORD}".encode(),
        hashlib.sha256
    ).hexdigest()[:32]
else:
    # Fallback to random token if credentials not set
    CALENDAR_TOKEN = os.getenv("CALENDAR_TOKEN", secrets.token_urlsafe(32))

DATA_DIR = Path("data")
SCHEDULE_FILE = DATA_DIR / "schedule.ics"

# Create data directory
DATA_DIR.mkdir(exist_ok=True)

# HTTP client with session management
client = httpx.AsyncClient(timeout=30.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    # Startup
    await scrape_schedule()
    
    # Schedule daily scraping at 6 AM
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scrape_schedule, 'cron', hour=6, minute=0)
    scheduler.start()
    
    print(f"[{datetime.now()}] Scheduler started. Daily scrape at 06:00")
    print(f"[{datetime.now()}] Your webcal URL: webcal://your-pi-address:8000/calendar/{CALENDAR_TOKEN}.ics")
    print(f"[{datetime.now()}] Or HTTP: http://your-pi-address:8000/calendar/{CALENDAR_TOKEN}.ics")
    
    yield
    
    # Shutdown
    scheduler.shutdown()
    await client.aclose()


# FastAPI app
app = FastAPI(title="TimeCare Webcal Service", lifespan=lifespan)


async def login_to_timecare():
    """Login to TimeCare Pool and return authenticated session"""
    try:
        # Get login page to extract ASP.NET ViewState and other hidden fields
        login_url = f"{TIMECARE_URL}/TimePoolWeb/Mobile/Login.aspx"
        login_page = await client.get(login_url)
        
        if login_page.status_code != 200:
            print(f"[{datetime.now()}] Failed to load login page: {login_page.status_code}")
            return False
        
        # Parse hidden form fields required by ASP.NET
        soup = BeautifulSoup(login_page.text, 'html.parser')
        
        # Extract all hidden fields (ViewState, ViewStateGenerator, EventValidation, etc.)
        hidden_fields = {}
        for hidden in soup.find_all('input', type='hidden'):
            if hidden.get('name') and hidden.get('value') is not None:
                hidden_fields[hidden['name']] = hidden['value']
        
        # Build login form data - use exact field names from the HTML
        login_data = {
            **hidden_fields,  # Include all ASP.NET hidden fields
            'ctl00$ContentMain$txtUserName': USERNAME,
            'ctl00$ContentMain$txtPassword': PASSWORD,
            'ctl00$ContentMain$btnLogin': 'Logga in',
        }
        
        # Add headers to mimic browser behavior
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': login_url,
        }
        
        # Submit login form
        response = await client.post(
            login_url,
            data=login_data,
            headers=headers,
            follow_redirects=True
        )
        
        # Debug: print response details
        print(f"[{datetime.now()}] Login response status: {response.status_code}")
        print(f"[{datetime.now()}] Login response URL: {response.url}")
        
        # Check if login was successful by looking for redirect or schedule page
        if response.status_code == 200:
            # Check if we're redirected away from login page
            if 'Login.aspx' not in str(response.url):
                print(f"[{datetime.now()}] Successfully logged in to TimeCare")
                return True
            else:
                # Still on login page - check for error message
                error_soup = BeautifulSoup(response.text, 'html.parser')
                validation_summary = error_soup.find('div', id='ctl00_ContentMain_ValidationSummary1')
                if validation_summary and validation_summary.get('style') != 'display:none;':
                    print(f"[{datetime.now()}] Login failed: {validation_summary.get_text(strip=True)}")
                else:
                    print(f"[{datetime.now()}] Login failed: Still on login page")
                return False
        else:
            print(f"[{datetime.now()}] Login failed: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"[{datetime.now()}] Login error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def scrape_schedule():
    """Scrape current week's schedule from TimeCare Pool"""
    print(f"[{datetime.now()}] Starting schedule scrape...")
    
    try:
        # Login
        if not await login_to_timecare():
            return
        
        # Get schedule page for current week
        today = datetime.now()
        
        # Fetch schedule - TimeCare shows the week when you pass a date
        schedule_url = f"{TIMECARE_URL}/TimePoolWeb/Mobile/Schedule.aspx"
        schedule_response = await client.get(
            schedule_url,
            params={
                "Date": today.strftime("%Y-%m-%d 00:00:00")
            }
        )
        
        if schedule_response.status_code != 200:
            print(f"[{datetime.now()}] Failed to fetch schedule: {schedule_response.status_code}")
            return
        
        # Parse schedule
        soup = BeautifulSoup(schedule_response.text, 'html.parser')
        
        schedule_entries = []
        
        # Find all shift entries (they're in collapsible divs)
        shifts = soup.find_all('div', attrs={'data-role': 'collapsible'})
        
        print(f"[{datetime.now()}] Found {len(shifts)} total shift entries")
        
        for shift in shifts:
            try:
                # Find the parent li to get the date from the listview id
                parent_ul = shift.find_parent('ul')
                if not parent_ul or not parent_ul.get('id'):
                    print(f"[{datetime.now()}] Debug: Shift has no parent ul with id, skipping")
                    continue
                
                # Extract date from ul id (e.g., "dayShifts-2025-10-17")
                ul_id = parent_ul['id']
                date_str = ul_id.replace('dayShifts-', '')
                
                # Get shift details from h6 header
                h6 = shift.find('h6')
                if not h6:
                    print(f"[{datetime.now()}] Debug: No h6 found in shift, skipping")
                    continue
                
                rows = h6.find_all('div', class_='calendarListRow')
                if len(rows) < 3:
                    print(f"[{datetime.now()}] Debug: Not enough rows ({len(rows)}), skipping")
                    continue
                
                # Parse shift type, time, and location
                shift_type = rows[0].get_text(strip=True)  # e.g., "Bokning" or "Tillgänglighet"
                
                # Filter out availability entries - only keep actual bookings
                if shift_type == "Tillgänglighet":
                    print(f"[{datetime.now()}] Debug: Skipping availability entry (not a booking)")
                    continue
                
                print(f"[{datetime.now()}] Debug: Processing {shift_type} for {date_str}")
                
                time_info = rows[1].get_text(strip=True)   # e.g., "08:30-16:30 Rast 30"
                location_code = rows[2].get_text(strip=True)    # e.g., "23 LärKan"
                
                # Parse time (format: "08:30-16:30")
                time_parts = time_info.split('\n')[0].strip()  # Get just the time part
                if '-' in time_parts:
                    start_time_str, end_time_str = time_parts.split('-')
                    start_time_str = start_time_str.strip()
                    end_time_str = end_time_str.strip()
                    
                    # Parse into datetime objects
                    start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
                    end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
                    
                    # Extract break time if present (e.g., "Rast 30")
                    break_time = ""
                    if "Rast" in time_info:
                        break_parts = time_info.split("Rast")
                        if len(break_parts) > 1:
                            break_time = f"Rast {break_parts[1].strip()} min"
                    
                    # Get full address/location details if available
                    address_elem = shift.find('a', id=lambda x: x and 'lnkAddress' in x)
                    full_address = ""
                    if address_elem:
                        full_address = address_elem.get_text(strip=True)
                    
                    # Get additional notes/description
                    notes = []
                    content_divs = shift.find_all('div', class_='calendarListRow')
                    for div in content_divs:
                        text = div.get_text(strip=True)
                        # Skip the already captured info
                        if text and text not in [shift_type, time_info, location_code] and not text.startswith('ID:'):
                            # Don't add the address again if it's in a link
                            if not div.find('a'):
                                notes.append(text)
                    
                    # Get shift ID if available
                    shift_id = ""
                    id_text = shift.get_text()
                    if "ID:" in id_text:
                        shift_id = id_text.split("ID:")[-1].strip().split()[0]
                    
                    # Build comprehensive description
                    description_parts = [shift_type]
                    if location_code:
                        description_parts.append(location_code)
                    if break_time:
                        description_parts.append(break_time)
                    description = " - ".join(description_parts)
                    
                    # Build comprehensive location
                    location_parts = []
                    if full_address:
                        location_parts.append(full_address)
                    elif location_code:
                        location_parts.append(location_code)
                    location_str = ", ".join(location_parts)
                    
                    # Build notes section
                    notes_str = "\n".join(notes) if notes else ""
                    if shift_id:
                        notes_str += f"\nID: {shift_id}" if notes_str else f"ID: {shift_id}"
                    
                    schedule_entries.append({
                        'start': start_dt,
                        'end': end_dt,
                        'location': location_str,
                        'summary': description,
                        'description': notes_str,
                    })
                    
                    print(f"[{datetime.now()}] Debug: Added event: {description} at {start_dt}")
                    
            except Exception as e:
                print(f"[{datetime.now()}] Error parsing shift: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        # Generate iCal file with merged events
        await generate_ical(schedule_entries)
        
        # Save historical copy
        history_file = DATA_DIR / f"schedule_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ics"
        if SCHEDULE_FILE.exists():
            import shutil
            shutil.copy(SCHEDULE_FILE, history_file)
        
        print(f"[{datetime.now()}] Schedule scrape completed. Found {len(schedule_entries)} booking shifts.")
        
    except Exception as e:
        print(f"[{datetime.now()}] Error during scrape: {e}")
        import traceback
        traceback.print_exc()


async def generate_ical(schedule_entries):
    """Generate iCal file from schedule entries, merging with existing events"""
    
    # Load existing calendar if it exists
    existing_events = {}
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE, 'rb') as f:
                existing_cal = Calendar.from_ical(f.read())
                for component in existing_cal.walk():
                    if component.name == "VEVENT":
                        uid = str(component.get('uid'))
                        existing_events[uid] = component
            print(f"[{datetime.now()}] Loaded {len(existing_events)} existing events")
        except Exception as e:
            print(f"[{datetime.now()}] Could not load existing calendar: {e}")
    
    # Create new calendar
    cal = Calendar()
    cal.add('prodid', '-//TimeCare Pool Schedule//EN')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Work Schedule')
    cal.add('x-wr-timezone', 'Europe/Stockholm')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    
    # Track new event UIDs
    new_event_uids = set()
    
    # Add new events
    for entry in schedule_entries:
        event = Event()
        event.add('summary', entry.get('summary', 'Work Shift'))
        event.add('dtstart', entry['start'])
        event.add('dtend', entry['end'])
        
        # Add location
        if entry.get('location'):
            event.add('location', entry['location'])
        
        # Add description with all the extra details
        if entry.get('description'):
            event.add('description', entry['description'])
        
        event.add('dtstamp', datetime.now())
        
        # Generate unique ID for each event
        uid = f"{entry['start'].strftime('%Y%m%d%H%M%S')}@timepool.boras.se"
        event.add('uid', uid)
        new_event_uids.add(uid)
        
        # Mark as busy time
        event.add('transp', 'OPAQUE')
        
        cal.add_component(event)
    
    # Add existing events that are not in the new set (to preserve history)
    # Only keep events that are in the past or near future
    cutoff_date = datetime.now() - timedelta(days=90)  # Keep last 90 days
    merged_count = 0
    
    for uid, old_event in existing_events.items():
        if uid not in new_event_uids:
            # Check if event is recent enough to keep
            dtstart = old_event.get('dtstart')
            if dtstart and hasattr(dtstart.dt, 'date'):
                event_date = dtstart.dt if isinstance(dtstart.dt, datetime) else datetime.combine(dtstart.dt, datetime.min.time())
                if event_date >= cutoff_date:
                    cal.add_component(old_event)
                    merged_count += 1
    
    print(f"[{datetime.now()}] Merged {merged_count} historical events (kept last 90 days)")
    
    # Write to file
    with open(SCHEDULE_FILE, 'wb') as f:
        f.write(cal.to_ical())
    
    total_events = len(schedule_entries) + merged_count
    print(f"[{datetime.now()}] Generated iCal file with {len(schedule_entries)} new events and {merged_count} historical events (total: {total_events})")


@app.get("/")
async def root():
    """Root endpoint with basic info"""
    return {
        "service": "TimeCare Pool Webcal",
        "status": "running",
        "last_update": datetime.fromtimestamp(SCHEDULE_FILE.stat().st_mtime).isoformat() if SCHEDULE_FILE.exists() else None
    }


@app.get("/calendar/{token}.ics")
async def get_calendar(token: str):
    """Serve the calendar feed with token authentication"""
    if token != CALENDAR_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    
    if not SCHEDULE_FILE.exists():
        raise HTTPException(status_code=503, detail="Calendar not yet generated")
    
    with open(SCHEDULE_FILE, "rb") as f:
        cal_data = f.read()
    
    return Response(
        content=cal_data,
        media_type="text/calendar",
        headers={
            "Content-Disposition": "inline; filename=schedule.ics",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        }
    )


@app.post("/refresh")
async def manual_refresh(token: str):
    """Manual refresh endpoint (also protected by token)"""
    if token != CALENDAR_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")
    
    await scrape_schedule()
    return {"status": "refresh triggered"}


@app.get("/debug-login")
async def debug_login():
    """Debug endpoint to test login and save form data"""
    try:
        login_url = f"{TIMECARE_URL}/TimePoolWeb/Mobile/Login.aspx"
        login_page = await client.get(login_url)
        
        soup = BeautifulSoup(login_page.text, 'html.parser')
        hidden_fields = {}
        for hidden in soup.find_all('input', type='hidden'):
            if hidden.get('name'):
                hidden_fields[hidden['name']] = hidden.get('value', '')
        
        return {
            "status": "login page loaded",
            "status_code": login_page.status_code,
            "hidden_fields": hidden_fields,
            "form_action": soup.find('form')['action'] if soup.find('form') else None,
        }
    except Exception as e:
        return {"error": str(e)}


def main():
    """Main entrypoint for the application"""
    # Save token to a file for reference if it was auto-generated
    token_file = Path("calendar_token.txt")
    if not token_file.exists():
        with open(token_file, "w") as f:
            f.write(f"Your calendar token: {CALENDAR_TOKEN}\n")
            f.write(f"Your webcal URL: webcal://your-pi-address:8000/calendar/{CALENDAR_TOKEN}.ics\n")
        print(f"[{datetime.now()}] Token saved to calendar_token.txt")
    
    # Run the FastAPI app
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

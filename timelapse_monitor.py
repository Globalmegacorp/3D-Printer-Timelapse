import subprocess
import time
import requests
import json
import csv
import os
import sys

# --- CONFIGURATION LOADING ---
CONFIG_FILE = 'config.json'

try:
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"FATAL ERROR: Configuration file '{CONFIG_FILE}' not found.")
    sys.exit(1)
except json.JSONDecodeError:
    print(f"FATAL ERROR: Configuration file '{CONFIG_FILE}' is not valid JSON.")
    sys.exit(1)

# API Headers
API_HEADERS = {"X-Api-Key": config['API_KEY']}

# Global variables for file paths (will be set in run_monitor)
RECORDED_VIDEO_PATH = ""
LOG_FILE_PATH = ""
SESSION_DIR = ""

# --- CONSTANTS ---
# Minimum Nozzle Target temperature to assume printing has started (i.e., finished probing/pre-heat)
MIN_PRINT_TEMP_TARGET = 190.0 
# The tolerance (N-2) applied to the Z, Bed Temp, and Nozzle Temp (Actual) checks
START_TOLERANCE = 2.0 

# --- API UTILITIES ---

def fetch_printer_status():
    """Fetches real-time printer status (position, temps, state)."""
    try:
        response = requests.get(config['PRINTER_API_URL'], headers=API_HEADERS, timeout=1.0)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        # Handle connection errors, timeouts, and non-2xx status codes
        print(f"API Error fetching printer status. Retrying.")
        return None

def fetch_job_details():
    """Fetches the current job details."""
    try:
        response = requests.get(config['JOB_API_URL'], headers=API_HEADERS, timeout=1.0)
        response.raise_for_status()
        
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    except requests.exceptions.RequestException as e:
        return None

# --- MONITORING LOGIC ---

def run_monitor():
    """Main function to monitor the print and record video."""
    global RECORDED_VIDEO_PATH, LOG_FILE_PATH, SESSION_DIR

    print("--- Fixed-Bed Timelapse Monitor ---")

    # 1. Check for FFmpeg
    try:
        subprocess.run([config['FFMPEG_CMD'], "-version"], check=True, capture_output=True)
    except FileNotFoundError:
        print(f"FATAL ERROR: The command '{config['FFMPEG_CMD']}' was not found.")
        sys.exit(1)
        
    # 2. Wait for print start
    job_details = fetch_job_details()
    # Check for 'PRINTING' state (uppercase)
    if not job_details or job_details.get('state') != 'PRINTING':
        print("Waiting for a print job to start...")
        while job_details is None or job_details.get('state') != 'PRINTING':
            time.sleep(5)
            job_details = fetch_job_details()
        print("Print job detected. Starting pre-print checks.")
    
    # 3. Define Session Directory and Paths
    if job_details and job_details.get('file', {}).get('display_name'):
        print_filename_base = job_details['file']['display_name']
    else:
        print_filename_base = f"print_session_{int(time.time())}"
        
    # Sanitize name
    SESSION_DIR = print_filename_base.replace(' ', '_').replace('.', '_').strip()
    
    RECORDED_VIDEO_PATH = os.path.join(SESSION_DIR, "print_recording.mp4")
    LOG_FILE_PATH = os.path.join(SESSION_DIR, "print_log.csv")
    
    os.makedirs(SESSION_DIR, exist_ok=True)
    print(f"Session directory created: {SESSION_DIR}")
    
    # 5. Wait for target conditions
    required_z_limit = config.get('REQUIRED_Z_CAPTURE_POS', 10.0)
    
    # Initialize targets using config defaults
    required_bed_temp = config.get('REQUIRED_BED_TEMP', 50.0)
    required_nozzle_temp = config.get('REQUIRED_NOZZLE_TEMP', 220.0)
    
    # Pre-calculate tolerance limits for display and checks
    required_z_limit_tol = required_z_limit - START_TOLERANCE
    required_nozzle_target_temp_for_check = required_nozzle_temp - START_TOLERANCE
    required_bed_target_temp_for_check = required_bed_temp - START_TOLERANCE

    print(f"Waiting for print conditions (Z < {required_z_limit_tol:.2f}mm, Bed Temp >= {required_bed_target_temp_for_check:.1f}°C, Nozzle Target >= {MIN_PRINT_TEMP_TARGET:.1f}°C)...")

    while True:
        status = fetch_printer_status()
        if not status:
            time.sleep(2)
            continue
            
        # --- KEY EXTRACTION ---
        printer_data = status.get('printer', {})
        position_z = printer_data.get('axis_z', 999.0)
        bed_temp = printer_data.get('temp_bed', 0)
        nozzle_temp = printer_data.get('temp_nozzle', 0)
        printer_state = printer_data.get('state', 'Unknown')
        
        # --- DYNAMIC TARGET SETTING (Check every time) ---
        target_bed = printer_data.get('target_bed')
        target_nozzle = printer_data.get('target_nozzle')
        
        # Update required targets if valid values are read from the API
        if target_bed is not None and target_bed > 0:
            required_bed_temp = target_bed
        
        if target_nozzle is not None and target_nozzle > 0:
            required_nozzle_temp = target_nozzle
            
        # Re-calculate tolerance limits based on updated targets
        required_z_limit_tol = required_z_limit - START_TOLERANCE
        required_nozzle_target_temp_for_check = required_nozzle_temp - START_TOLERANCE
        required_bed_target_temp_for_check = required_bed_temp - START_TOLERANCE
            
        # --- Check for Start Conditions (Applying N-2 tolerance) ---
        z_ok = position_z < required_z_limit_tol
        bed_ok = bed_temp >= required_bed_target_temp_for_check
        nozzle_actual_ok = nozzle_temp >= required_nozzle_target_temp_for_check
        
        # Target temperature check (No tolerance here, must be high enough to assume printing)
        target_temp_ok = required_nozzle_temp >= MIN_PRINT_TEMP_TARGET
        
        # Check for print end status
        if printer_state in ['FINISHED', 'ERROR']:
            print(f"Print ended prematurely or errored. Stopping monitor.")
            sys.exit(0)
        
        if z_ok and bed_ok and nozzle_actual_ok and target_temp_ok:
            print(f"\nPre-print conditions met. (Z: {position_z:.2f}, Bed: {bed_temp:.1f}, Nozzle: {nozzle_temp:.1f}). Starting recording and logging.")
            break
            
        # Displaying status for the user
        z_stat = f"Z={position_z:.3f} (Need < {required_z_limit_tol:.3f}, {'OK' if z_ok else 'WAIT'})"
        bed_stat = f"Bed={bed_temp}°C (Need >= {required_bed_target_temp_for_check:.1f}°C, {'OK' if bed_ok else 'WAIT'})"
        noz_stat = f"Nozzle={nozzle_temp}°C (Need >= {required_nozzle_target_temp_for_check:.1f}°C, {'OK' if nozzle_actual_ok else 'WAIT'})"
        target_stat = f"Target Noz={required_nozzle_temp:.1f}°C (Need >= {MIN_PRINT_TEMP_TARGET:.1f}°C, {'OK' if target_temp_ok else 'PROBING'})         "
        
        print(f"Status: {z_stat}, {bed_stat}, {noz_stat}, {target_stat}", end='\r', flush=True)
        
        time.sleep(config.get('POLL_INTERVAL', 1.0))

    # 6. Start Video Recording (FFmpeg process)
    video_cmd = [
        config['FFMPEG_CMD'],
        "-y", 
        "-loglevel", "quiet",
        "-rtsp_transport", "tcp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-probesize", "32",
        "-analyzeduration", "1000000",
        "-i", config['RTSP_STREAM_URL'], 
        "-c:v", "copy",
        "-copyts",
        "-avoid_negative_ts", "make_zero",
        "-flush_packets", "1",
        RECORDED_VIDEO_PATH
    ]
    
    print(f"\nStarting video recording: {RECORDED_VIDEO_PATH}...")
    video_process = subprocess.Popen(video_cmd, 
                                     stdin=subprocess.PIPE, 
                                     stdout=subprocess.DEVNULL, 
                                     stderr=subprocess.DEVNULL)
    
    start_time = time.time()
    # 7. Start Logging
    log_file_exists = os.path.exists(LOG_FILE_PATH)
    with open(LOG_FILE_PATH, 'a', newline='') as log_file:
        writer = csv.writer(log_file)
        if not log_file_exists or os.path.getsize(LOG_FILE_PATH) == 0:
            writer.writerow(['RelativeTimestamp', 'State', 'Z', 'TempBed', 'TempNozzle'])
            
        print("Starting active logging...")
        
        while True:
            status = fetch_printer_status()
            elapsed_time = time.time() - start_time
            
            if status:
                # --- KEY EXTRACTION for logging ---
                printer_data = status.get('printer', {})
                position_z = printer_data.get('axis_z', 999.0)
                bed_temp = printer_data.get('temp_bed', 0)
                nozzle_temp = printer_data.get('temp_nozzle', 0)
                printer_state = printer_data.get('state', 'Unknown')
                
                writer.writerow([
                    f"{elapsed_time:.3f}", 
                    printer_state, 
                    f"{position_z:.3f}", 
                    f"{bed_temp:.1f}", 
                    f"{nozzle_temp:.1f}"
                ])
                log_file.flush()
                
                print(f"Log: {elapsed_time:.2f}s | State: {printer_state} | Z: {position_z:.3f}mm", end='\r', flush=True)

                # CHECK FOR UPPERCASE END/ERROR STATUS
                if printer_state in ['FINISHED', 'ERROR', 'CANCELED']:
                    print(f"\nPrint finished/errored ({printer_state}). Stopping recording.")
                    break

            time.sleep(config.get('POLL_INTERVAL', 1.0))

    # 8. Stop FFmpeg Recording
    try:
        video_process.communicate(input=b'q')
        video_process.wait(timeout=10)
        print(f"Video recording saved to {RECORDED_VIDEO_PATH}.")
    except Exception as e:
        print(f"Error stopping video process: {e}. Killing process.")
        video_process.terminate()

    print("\nMonitor script finished. Run post-processor next using the session directory:")
    print(f"python3 printer_timelapse_generator.py {SESSION_DIR}")

if __name__ == "__main__":
    run_monitor()


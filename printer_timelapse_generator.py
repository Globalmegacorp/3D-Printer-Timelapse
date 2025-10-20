import subprocess
import json
import csv
import os
import sys
import shutil
import statistics
from collections import defaultdict

# --- CONFIGURATION LOADING ---
CONFIG_FILE = 'config.json'
# Internal working files and directories
RECORDED_VIDEO_PATH_BASE = "print_recording.mp4"
LOG_FILE_PATH_BASE = "print_log.csv"
FRAME_DIR = "extracted_frames"

# This will be populated after os.chdir
FULL_CONFIG_PATH = ''

try:
    # We need to find the config relative to the script's execution path
    # because we change directories later.
    original_cwd = os.path.dirname(os.path.abspath(sys.argv[0]))
    FULL_CONFIG_PATH = os.path.join(original_cwd, CONFIG_FILE)
    with open(FULL_CONFIG_PATH, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"FATAL ERROR: Configuration file '{CONFIG_FILE}' not found in script directory.")
    sys.exit(1)
except json.JSONDecodeError:
    print(f"FATAL ERROR: Configuration file '{CONFIG_FILE}' is not valid JSON.")
    sys.exit(1)

# Dynamic FFmpeg options constructed from config
FFMPEG_TIMELAPSE_FRAMERATE = str(config['FFMPEG_TIMELAPSE_FRAMERATE'])
PRINT_FILENAME_BASE = "default_print_timelapse"


# --- FRAME EXTRACTION AND CORRUPTION HANDLING ---

def extract_single_frame(video_path, timestamp, output_path):
    """Extracts a single frame using a robust FFmpeg command."""
    extract_cmd = [
        config['FFMPEG_CMD'], '-y', '-ss', str(timestamp),
        '-i', video_path, '-an', '-vframes', '1',
        '-q:v', '1', output_path
    ]
    result = subprocess.run(extract_cmd, capture_output=True, text=True)
    return result.returncode == 0 and os.path.exists(output_path)

def detect_and_replace_corrupt_frames(frame_dir, layer_timestamps, video_path):
    """
    Analyzes extracted frames based on filesize and attempts to replace corrupt ones.
    """
    print("\n--- Starting Corruption Detection and Replacement ---")
    
    frame_paths = sorted([os.path.join(frame_dir, f) for f in os.listdir(frame_dir) if f.startswith('frame_')])
    if not frame_paths:
        print("No frames found to analyze.")
        return

    filesizes = [os.path.getsize(p) for p in frame_paths]
    if not filesizes:
        print("Could not get filesizes for analysis.")
        return

    median_size = statistics.median(filesizes)
    threshold_ratio = config.get('CORRUPTION_SIZE_THRESHOLD_RATIO', 0.88)
    size_threshold = median_size * threshold_ratio

    print(f"Median frame size: {median_size} bytes. Corruption threshold: {size_threshold:.0f} bytes.")

    corrupt_frames_found = 0
    for i, frame_path in enumerate(frame_paths):
        size = filesizes[i]
        if size < size_threshold:
            corrupt_frames_found += 1
            z_layer_key = sorted(layer_timestamps.keys())[i]
            
            print(f"  - Corrupt frame detected: {os.path.basename(frame_path)} (Size: {size} bytes)")

            # Attempt to re-extract using the next available timestamp for this layer
            # The initial extraction used index 0. We try subsequent indices.
            available_ts = layer_timestamps[z_layer_key]
            replaced = False
            for attempt_idx in range(1, len(available_ts)):
                new_ts = available_ts[attempt_idx]
                print(f"    -> Retrying with next timestamp for Z={z_layer_key:.2f}: {new_ts:.2f}s")
                if extract_single_frame(video_path, new_ts, frame_path):
                    new_size = os.path.getsize(frame_path)
                    print(f"    -> SUCCESS! Replaced frame. New size: {new_size} bytes.")
                    if new_size >= size_threshold:
                        replaced = True
                        break # Stop trying for this frame
            
            if not replaced:
                print(f"    -> FAILED to find a valid replacement frame for {os.path.basename(frame_path)}.")
    
    if corrupt_frames_found == 0:
        print("No corrupt frames detected.")


# --- DATA PROCESSING ---

def process_logs_and_extract_frames(session_dir):
    """
    Reads the log, finds all timestamps for each Z layer, and extracts the first valid frame.
    Returns: path to the last valid frame.
    """
    LOG_FILE_PATH = os.path.join(session_dir, LOG_FILE_PATH_BASE)
    RECORDED_VIDEO_PATH = os.path.join(session_dir, RECORDED_VIDEO_PATH_BASE)
    
    if not all(os.path.exists(p) for p in [LOG_FILE_PATH, RECORDED_VIDEO_PATH]):
        print(f"Error: Log or video file not found in {session_dir}.")
        return None

    frame_output_dir = os.path.join(session_dir, FRAME_DIR)
    if os.path.exists(frame_output_dir):
        shutil.rmtree(frame_output_dir)
    os.makedirs(frame_output_dir, exist_ok=True)
    
    print(f"\n--- Starting Initial Log Processing and Frame Extraction ---")

    # Structure to hold ALL timestamps for each layer: {Z_float: [ts1, ts2, ...]}
    layer_timestamps = defaultdict(list)
    
    try:
        with open(LOG_FILE_PATH, 'r', newline='') as log_file:
            reader = csv.DictReader(log_file)
            for row in reader:
                try:
                    rel_ts = float(row['RelativeTimestamp'])
                    z = round(float(row['Z']), 2)
                    layer_timestamps[z].append(rel_ts)
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"Failed to read or parse log file: {e}")
        return None

    if 0.00 in layer_timestamps:
        del layer_timestamps[0.00]
        
    sorted_z_layers = sorted(layer_timestamps.keys())
    if not sorted_z_layers:
        print("No valid Z layer data found.")
        return None
        
    frame_count = 0
    last_frame_path = None
    
    for z in sorted_z_layers:
        # Use the first timestamp for the initial extraction attempt
        start_ts = layer_timestamps[z][0]
        
        frame_count += 1
        output_frame_path = os.path.join(frame_output_dir, f"frame_Z_{frame_count:06d}.png")

        print(f"Z={z:.2f} | Extracting frame {frame_count} at Log TS: {start_ts:.2f}s")
        if extract_single_frame(RECORDED_VIDEO_PATH, start_ts, output_frame_path):
            last_frame_path = output_frame_path
            print(f"  -> Saved frame to {output_frame_path}")
        else:
            print(f"  !!! ERROR extracting initial frame {frame_count} for Z={z}")

    print(f"\nInitial extraction complete. Extracted {frame_count} frames.")
    
    # Run the corruption check and replacement phase
    detect_and_replace_corrupt_frames(frame_output_dir, layer_timestamps, RECORDED_VIDEO_PATH)
    
    # Find the last valid frame again after potential replacements
    final_frames = sorted([f for f in os.listdir(frame_output_dir) if f.startswith('frame_')])
    return os.path.join(frame_output_dir, final_frames[-1]) if final_frames else None


# --- TIMELAPSE ASSEMBLY ---

def assemble_timelapse(session_dir, last_frame_path):
    """Stitches frames into a final video, using tpad to hold the last frame."""
    frame_output_dir = os.path.join(session_dir, FRAME_DIR)
    if not os.path.exists(frame_output_dir) or not os.listdir(frame_output_dir):
        print(f"Error: No frames found in {frame_output_dir}.")
        return

    print("\n--- Starting Final Timelapse Assembly ---")
    final_output_name = f"{PRINT_FILENAME_BASE}_timelapse.mp4" 

    assembly_cmd = [
        config['FFMPEG_CMD'],
        "-y",
        "-framerate", FFMPEG_TIMELAPSE_FRAMERATE,
        "-i", os.path.join(frame_output_dir, "frame_Z_%06d.png"),
        "-vf", "tpad=stop_mode=clone:stop_duration=5", # Pad last frame for 5s
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        final_output_name
    ]
    
    print(f"Assembling video into {final_output_name}...")
    
    try:
        result = subprocess.run(assembly_cmd, check=True, capture_output=True, text=True)
        print(f"\n*** SUCCESS! Timelapse video saved as {final_output_name} ***")
    except subprocess.CalledProcessError as e:
        print(f"\n!!! ERROR during timelapse assembly:")
        print(e.stderr)

# --- MAIN EXECUTION ---

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 fixed_bed_postprocessor.py <SESSION_DIRECTORY>")
        sys.exit(1)
        
    session_dir = sys.argv[1]
    
    try:
        os.chdir(session_dir)
    except FileNotFoundError:
        print(f"FATAL ERROR: Session directory '{session_dir}' not found.")
        sys.exit(1)
        
    # Set Filename Base directly from the session directory name
    PRINT_FILENAME_BASE = os.path.basename(os.getcwd()).replace('.', '_').strip()
    print(f"--- Post-Processing Session: {PRINT_FILENAME_BASE} ---")
        
    last_frame = process_logs_and_extract_frames(os.getcwd())

    if last_frame:
        assemble_timelapse(os.getcwd(), last_frame)
    else:
        print("Skipping assembly: Frame extraction failed or found no data.")



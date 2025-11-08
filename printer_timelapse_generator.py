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

# Config options are read as needed from the 'config' dict
PRINT_FILENAME_BASE = "default_print_timelapse"


# --- FRAME EXTRACTION AND CORRUPTION HANDLING ---

def extract_single_frame(video_path, timestamp, output_path):
    """Extracts a single frame using a robust FFmpeg command."""
    # Use fast seek (-ss before -i) is crucial for performance. -an (no audio) and -y (overwrite) are added.
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
        
        # Ensure index i is valid for the sorted Z layer keys
        sorted_z_layers = sorted(layer_timestamps.keys())
        if i >= len(sorted_z_layers):
             print(f"Warning: Frame index {i} exceeds available layer data. Skipping corruption check.")
             continue

        if size < size_threshold:
            corrupt_frames_found += 1
            z_layer_key = sorted_z_layers[i]
            
            print(f"  - Corrupt frame detected: {os.path.basename(frame_path)} (Size: {size} bytes)")

            # Attempt to re-extract using the next available timestamp for this layer
            available_ts = layer_timestamps[z_layer_key]
            replaced = False
            # We iterate through ALL available timestamps as retry candidates
            for attempt_idx in range(0, len(available_ts)):
                new_ts = available_ts[attempt_idx]
                print(f"    -> Retrying with timestamp for Z={z_layer_key:.2f}: {new_ts:.2f}s")
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
    Reads the log, filters for stable Z layers, finds the midpoint timestamp for each,
    and extracts the frame.
    Returns: path to the last valid frame.
    """
    LOG_FILE_PATH = os.path.join(session_dir, LOG_FILE_PATH_BASE)
    RECORDED_VIDEO_PATH = os.path.join(session_dir, RECORDED_VIDEO_PATH_BASE)
    
    if not all(os.path.exists(p) for p in [LOG_FILE_PATH, RECORDED_VIDEO_PATH]):
        print(f"Error: Log or video file not found in {session_dir}. Cannot extract frames.")
        return None, None

    frame_output_dir = os.path.join(session_dir, FRAME_DIR)
    # Clear directory only if we are forced to re-extract
    if os.path.exists(frame_output_dir):
        shutil.rmtree(frame_output_dir)
    os.makedirs(frame_output_dir, exist_ok=True)
    
    print(f"\n--- Starting Initial Log Processing and Frame Extraction ---")

    # Structure to hold ALL timestamps for each layer: {Z_float: [ts1, ts2, ...]}
    layer_timestamps_raw = defaultdict(list)
    
    try:
        with open(LOG_FILE_PATH, 'r', newline='') as log_file:
            reader = csv.DictReader(log_file)
            for row in reader:
                try:
                    rel_ts = float(row['RelativeTimestamp'])
                    # Round Z to 3 decimal places for precision matching
                    z = round(float(row['Z']), 3) 
                    layer_timestamps_raw[z].append(rel_ts)
                except (ValueError, KeyError, TypeError):
                    continue
    except Exception as e:
        print(f"Failed to read or parse log file: {e}")
        return None, None

    # --- Filter for Stable Layers (using config settings) ---
    print("Filtering for valid and stable Z layers...")
    stable_layer_timestamps = {}
    last_z = 0.0
    
    min_z_change = config.get('MIN_Z_CHANGE_MM', 0.1)
    max_z_change = config.get('MAX_LAYER_HEIGHT_MM', 1.0)
    min_stability = config.get('MIN_STABILITY_COUNT', 3)

    for z in sorted(layer_timestamps_raw.keys()):
        if z <= 0.0:
            continue
            
        ts_list = layer_timestamps_raw[z]
        z_change = z - last_z

        is_stable = len(ts_list) >= min_stability
        is_valid_layer_change = min_z_change <= z_change <= max_z_change

        if is_stable and is_valid_layer_change:
            stable_layer_timestamps[z] = ts_list
            last_z = z # Update last_z only when a valid layer is found
        else:
            print(f"  - Skipping Z={z:.3f} (Change: {z_change:.3f}mm, Stable: {is_stable}, ValidZ: {is_valid_layer_change})")

    if not stable_layer_timestamps:
        print("No valid Z layer data found after filtering.")
        return None, None
        
    print(f"Found {len(stable_layer_timestamps)} stable Z layers to process.")
    
    # --- Initial Frame Extraction ---
    frame_count = 0
    last_frame_path = None
    
    sorted_z_layers = sorted(stable_layer_timestamps.keys())
    
    for z in sorted_z_layers:
        ts_list = stable_layer_timestamps[z]
        start_ts = ts_list[0]
        end_ts = ts_list[-1]
        
        # Calculate the midpoint timestamp for the layer
        mid_ts = start_ts + (end_ts - start_ts) / 2
        
        frame_count += 1
        output_frame_path = os.path.join(frame_output_dir, f"frame_Z_{frame_count:06d}.png")

        print(f"Z={z:.3f} | Layer dur: {end_ts - start_ts:.2f}s | Extracting frame {frame_count} at Mid-Point TS: {mid_ts:.2f}s")
        if extract_single_frame(RECORDED_VIDEO_PATH, mid_ts, output_frame_path):
            last_frame_path = output_frame_path
            print(f"  -> Saved frame to {output_frame_path}")
        else:
            print(f"  !!! ERROR extracting initial frame {frame_count} for Z={z}")

    print(f"\nInitial extraction complete. Extracted {frame_count} frames.")
    
    # Run the corruption check and replacement phase
    # Pass the stable_layer_timestamps dict so it can access all retry timestamps
    detect_and_replace_corrupt_frames(frame_output_dir, stable_layer_timestamps, RECORDED_VIDEO_PATH)
    
    # Find the last valid frame again after potential replacements
    final_frames = sorted([f for f in os.listdir(frame_output_dir) if f.startswith('frame_')])
    final_last_frame_path = os.path.join(frame_output_dir, final_frames[-1]) if final_frames else None

    return final_last_frame_path, stable_layer_timestamps


# --- ASSEMBLY UTILITY ---

def get_last_frame_path(session_dir):
    """
    Checks the FRAME_DIR and returns the path to the last extracted frame.
    If the directory or frames don't exist, returns None.
    """
    frame_output_dir = os.path.join(session_dir, FRAME_DIR)
    if not os.path.exists(frame_output_dir):
        return None
    
    # Check if the directory has files
    final_frames = sorted([f for f in os.listdir(frame_output_dir) if f.startswith('frame_')])
    if not final_frames:
        return None
        
    return os.path.join(frame_output_dir, final_frames[-1])

def check_existing_frames(session_dir):
    """Checks if frames have already been extracted."""
    frame_output_dir = os.path.join(session_dir, FRAME_DIR)
    
    if os.path.exists(frame_output_dir):
        frame_count = len([f for f in os.listdir(frame_output_dir) if f.startswith('frame_')])
        if frame_count > 0:
            print(f"Found {frame_count} previously extracted frames. Skipping frame extraction and proceeding directly to assembly.")
            return True
    return False

# --- TIMELAPSE ASSEMBLY ---

def assemble_timelapse(session_dir, last_frame_path, calculated_framerate_str):
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
        "-framerate", calculated_framerate_str, # Use the new dynamic framerate
        "-i", os.path.join(frame_output_dir, "frame_Z_%06d.png"),
        # Use tpad filter for stable last frame repetition
        "-vf", "tpad=stop_mode=clone:stop_duration=5", 
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        final_output_name
    ]
    
    print(f"Assembling video with {calculated_framerate_str}fps into {final_output_name}...")
    
    try:
        # Use subprocess.run without check=True to prevent crash on non-fatal FFmpeg warnings,
        # but check return code afterwards.
        result = subprocess.run(assembly_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"\n!!! ERROR during timelapse assembly (Code {result.returncode}):")
            print(result.stderr)
            return

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
    
    last_frame = None
    
    # Check if frames already exist
    frames_exist = check_existing_frames(os.getcwd())
    
    if frames_exist:
        last_frame = get_last_frame_path(os.getcwd())
        # Note: We don't need layer_timestamps if we skip extraction
    else:
        # Process logs, extract frames, check for corruption
        last_frame, layer_data = process_logs_and_extract_frames(os.getcwd())
        
    if last_frame:
        # --- NEW DYNAMIC FRAMERATE LOGIC ---
        frame_output_dir = os.path.join(os.getcwd(), FRAME_DIR)
        frame_files = [f for f in os.listdir(frame_output_dir) if f.startswith('frame_')]
        num_frames = len(frame_files)

        if num_frames == 0:
            print("Error: No frames found for assembly.")
            sys.exit(1)

        target_duration_s = 10.0
        # Calculate framerate as a whole integer
        calculated_framerate = int(num_frames / target_duration_s)
        
        min_fr = config.get('MIN_FRAMERATE', 15)
        max_fr = config.get('MAX_FRAMERATE', 60)
        
        # Clamp the calculated framerate between min and max
        final_framerate = max(min_fr, min(max_fr, calculated_framerate))
        final_framerate_str = str(final_framerate)

        print(f"\n--- Dynamic Framerate Calculation ---")
        print(f"Found {num_frames} frames. Target duration: {target_duration_s}s.")
        print(f"Calculated raw framerate (frames/duration): {calculated_framerate}fps.")
        print(f"Clamping to min/max ({min_fr}/{max_fr})...")
        print(f"Final assembly framerate: {final_framerate}fps.")
        
        assemble_timelapse(os.getcwd(), last_frame, final_framerate_str)
    else:
        print("Skipping assembly: Frame extraction failed or found no data.")

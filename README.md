# **3D Printer Timelapse Generator**

## **1\. Project Purpose**

This project provides a suite of Python scripts to create high-quality, stable timelapses of 3D prints from printers that offer a JSON-based API (such as PrusaLink). It is designed specifically for **fixed-bed (Cartesian)** printers where the bed only moves along the Z-axis. It was developed and tested against a Prusa Core One, but assuming your printer has an API which exposes Z-axis height, and your camera has an rtsp stream, it should work for other printers too.

The process is split into two main stages:

1. **Monitoring:** A script actively monitors a printer's API and an RTSP camera stream. It waits for a print to start, records the video feed, and logs the printer's Z-axis position with timestamps.  
2. **Post-Processing:** A second script analyzes the recorded video and log files to extract one frame for each distinct layer of the print. It includes logic to handle common issues like video stream corruption and produces a final, smooth timelapse video.

## **2\. Design Decisions**

This project evolved significantly to overcome challenges related to video stream instability, API data quirks, and the physics of 3D printing. The key design choices are explained below.

### **Two-Stage Architecture (Monitor & Post-Processor)**

The workflow is intentionally split into timelapse\_monitor.py and fixed\_bed\_postprocessor.py.

* **Reliability:** This separation isolates the real-time data capture from the CPU-intensive video processing. The monitor's only job is to record data, making it lightweight and less prone to crashing during a long print.  
* **Iterability:** If you want to change the timelapse framerate, adjust corruption detection, or fix a bug in the processing logic, you can re-run the post-processor on the existing data without needing to re-record a multi-hour print.

### **Automated Print Start Detection**

To ensure the recording starts at the right moment (after pre-heating but before printing), the monitor script uses a multi-factor check:

* **Low Z-Position:** The Z-axis must be below a certain height (REQUIRED\_Z\_CAPTURE\_POS), indicating the print head is near the bed.
* **Stable Bed & Nozzle Temperature:** The script waits for the actual bed and nozzle temperatures to reach their targets (with a \-2.0°C tolerance to account for normal PID fluctuations).  
* **Target Nozzle Temperature Threshold:** The script also checks that the *target* nozzle temperature is above 190°C. This prevents the script from starting during the bed-probing phase, where the nozzle is often kept at a lower standby temperature.

### **Layer Detection via Z-Change**

For a fixed-bed printer, the most reliable indicator of a new layer is a change in the Z-axis.

* The post-processor analyzes the log file and groups all timestamps by their Z-height (rounded to two decimal places).  
* It looks for a Z-change greater than a minimum threshold (MIN\_Z\_CHANGE\_MM) to filter out minor fluctuations or non-print moves.  
* To ensure the Z-position is stable and not just a transient reading, it confirms the new Z-height is reported for a minimum number of consecutive measurements (MIN\_STABILITY\_COUNT).  
* The timestamp of the *first* time the new Z-height is reported is used as the capture point for that layer's frame.

### **Corrupt Frame Detection and Replacement**

RTSP streams can be unreliable, leading to extracted frames that are corrupted (e.g., a gray or partial image). These frames typically have a much smaller filesize than valid frames.

* The script first extracts all frames based on the log data.  
* It then calculates the **median file size** of all successfully generated PNG files.  
* It iterates through the frames again. Any frame with a file size less than a configurable ratio of the median (CORRUPTION\_SIZE\_THRESHOLD\_RATIO) is flagged as corrupt.  
* For each corrupt frame, the script attempts to re-extract a replacement using the *next available timestamp* from the log for that same Z-layer. This provides a simple but effective way to recover from transient stream glitches.

### **Session-Based File Management**

Each print job is treated as a unique session. The monitor script creates a dedicated directory named after the print job (e.g., My\_Awesome\_Print\_gcode/). All associated files: the log, the video recording, the extracted frames, and the final timelapse, are stored within this directory. This prevents file collisions and keeps the project organized.

### **Video Assembly and Finalization**

* **FFmpeg tpad Filter:** To create a visually pleasing end to the timelapse, the last successfully extracted frame is held for 5 seconds. This is achieved using FFmpeg's  tpad (temporal pad) filter.  
* **Stream Copy Optimization:** The monitor script uses \-c:v copy when recording. This directly copies the H.264 video stream from the camera without re-encoding, which dramatically reduces CPU load and prevents quality loss. Various flags (-reconnect, \-fflags nobuffer) are used to make this process resilient to network instability.

## **3\. Configuration (config.json)**

All user-configurable options are located in the config.json file.

| Option | Default Value | Description |
| :---- | :---- | :---- |
| PRINTER\_API\_URL | http://PRINTER_IP ADDRESS HERE/api/v1/status | The full URL to the printer's status API endpoint. |
| JOB\_API\_URL | http://PRINTER_IP ADDRESS HERE/api/v1/job | The full URL to the printer's job API endpoint. |
| API\_KEY | YOUR\_API\_KEY\_HERE | The API key required to authenticate with the printer's API. |
| RTSP\_STREAM\_URL | rtsp://CAMERA_IP_ADDRESS_HERE/live | The full RTSP URL for the camera's live video feed. |
| FFMPEG\_CMD | ffmpeg | The command to execute FFmpeg. Change this if ffmpeg is not in your system's PATH. |
| POLL\_INTERVAL | 1.0 | The time in seconds between API poll requests during monitoring. |
| REQUIRED\_Z\_CAPTURE\_POS | 10.0 | The maximum Z-height (in mm) for the print to be considered "starting". |
| REQUIRED\_BED\_TEMP | 50.0 | The minimum bed temperature (in °C) required to start monitoring. |
| FFMPEG\_TIMELAPSE\_FRAMERATE | 30 | The output framerate for the final timelapse video. |
| MIN\_Z\_CHANGE\_MM | 0.1 | The minimum Z-axis change (in mm) to be considered a new layer. |
| MIN\_STABILITY\_COUNT | 3 | The number of consecutive measurements a new Z-height must be held for. |
| MAX\_LAYER\_HEIGHT\_MM | 1.0 | The maximum Z-axis change to be considered a valid layer (filters out final Z-lifts). |
| CORRUPTION\_SIZE\_THRESHOLD\_RATIO | 0.88 | A frame is corrupt if its size is less than this ratio of the median frame size. |

## **4\. How to Run**

### **Step 1: Initial Setup**

1. Ensure you have Python 3 and FFmpeg (including ffprobe) installed and available in your system's PATH.  
2. Install the required Python library: pip install requests.  
3. Copy config.json.example to config.json and fill in all the required values, especially your API key and IP addresses.

### **Step 2: Monitor a Print**

1. Before you start a print on your printer, run the monitor script from your terminal:  
   python3 timelapse\_monitor.py

2. The script will wait for the print job to start and the printer to reach the required temperatures.  
3. Once conditions are met, it will create a session directory (e.g., My\_Awesome\_Print\_gcode/) and begin recording the video and logging data.  
4. Let the script run until the print is complete. It will automatically stop recording. Note the session directory name it created.

### **Step 3: Post-Process the Timelapse**

1. After the print is finished and the monitor script has exited, run the post-processor script.  
2. Provide the **session directory name** that was created in Step 2 as a command-line argument.  
   python3 fixed\_bed\_postprocessor.py "My\_Awesome\_Print\_gcode"

3. The script will process the log, extract all the frames, check for and replace corrupt frames, and assemble the final timelapse video inside the session directory.

You can re-run the post-processor script on the same session directory multiple times to regenerate the video if you change settings like the output framerate.

from flask import Flask, render_template
import os
import platform
import psutil
import threading
import time
import subprocess

app = Flask(__name__)

# Global variables to store metrics
cpu_usage = 0
memory_usage = 0
disk_usage = 0

def update_metrics():
    """Continuously update system metrics in background"""
    global cpu_usage, memory_usage, disk_usage
    
    while True:
        cpu_usage = psutil.cpu_percent(interval=1)
        memory_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage('/').percent
        time.sleep(2)

# Start the metrics collection thread
metrics_thread = threading.Thread(target=update_metrics, daemon=True)
metrics_thread.start()

@app.route('/')
def index():
    """Main page showing system info and metrics"""
    system_info = {
        'hostname': platform.node(),
        'platform': platform.platform(),
        'processor': platform.processor(),
        'cpu_cores': psutil.cpu_count(),
        'memory_total': f"{psutil.virtual_memory().total / (1024**3):.2f} GB"
    }
    
    metrics = {
        'cpu': cpu_usage,
        'memory': memory_usage,
        'disk': disk_usage
    }
    
    return render_template('index.html', system_info=system_info, metrics=metrics)

@app.route('/load/cpu/<int:seconds>')
def load_cpu(seconds):
    """Generate CPU load using `stress` command for specified seconds"""
    if seconds > 180:  # Limit to 3 minutes
        seconds = 180
    
    try:
        # Run the `stress` command using subprocess
        subprocess.Popen(['stress', '--cpu', '3', '--timeout', str(seconds)])
        return f"Running CPU stress test for {seconds} seconds using `stress` command..."
    except FileNotFoundError:
        return "Error: `stress` tool is not installed on the system. Please install it using `sudo apt install stress`."

@app.route('/load/memory/<int:mb>')
def load_memory(mb):
    """Allocate specified MB of memory"""
    if mb > 1024:  # Limit to 1GB
        mb = 1024
        
    # Allocate memory (temporary, will be garbage collected)
    mem_data = bytearray(mb * 1024 * 1024)
    
    return f"Allocated {mb} MB of memory temporarily"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

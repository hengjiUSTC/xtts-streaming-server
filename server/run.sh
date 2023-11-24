if pgrep -f "main:app" > /dev/null ; then
    pkill -f "main:app"
fi

source venv/bin/activate

# Generate a timestamp
timestamp=$(date +"%Y%m%d_%H%M%S")

# Generate a new log filename with the timestamp
log_filename="demo_$timestamp.log"

# prod
nohup uvicorn main:app --host 0.0.0.0 --port 8000 >> $log_filename 2>&1 &
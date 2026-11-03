import os
from app import create_app
from app.config import Config

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    print(f"================================================================")
    print(f" Smart Bus Passenger Tracking & Attendance AI System")
    print(f" Server running at: http://127.0.0.1:{port}")
    print(f" Admin credentials:    admin / admin123")
    print(f" Operator credentials: operator / operator123")
    print(f"================================================================")
    app.run(host="0.0.0.0", port=port, debug=debug)

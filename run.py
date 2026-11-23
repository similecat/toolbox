"""Run the Flask app over HTTPS with an auto-generated self-signed TLS certificate."""
import os
from app import app
from ssl_utils import ensure_self_signed_cert

if __name__ == "__main__":
    port = int(os.environ.get("TOOLBOX_PORT", "5000"))
    ssl_dir = os.path.join(app.instance_path, "ssl")
    cert_path, key_path = ensure_self_signed_cert(ssl_dir)
    print(f"Starting HTTPS server on https://0.0.0.0:{port}")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
        ssl_context=(cert_path, key_path),
    )

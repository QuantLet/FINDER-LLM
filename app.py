"""Start FINDER Studio on this computer: python app.py."""

import argparse
from studio.server import serve


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Open FINDER Studio on this computer.')
    parser.add_argument('--port', type=int, default=8765, help='Local port (default: 8765).')
    args = parser.parse_args()
    serve(args.port)

import json
import logging
import os
import sys
from pathlib import Path
import threading
from typing import Dict, Optional, List

# Configure logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

import click
from flask import Flask, request, jsonify, render_template, send_from_directory
import importlib.resources as pkg_resources

from crewai_chat_ui.crew_loader import load_crew, load_crew_from_module, discover_available_crews
from crewai_chat_ui.chat_handler import ChatHandler


# Create Flask app
app = Flask(__name__)
chat_handler = None

# Dictionary to store cached chat handlers
chat_handlers: Dict[str, ChatHandler] = {}

# Stores discovered crews information
discovered_crews: List[Dict] = []


@app.route("/")
def index():
    """Serve the main chat interface."""
    static_dir = Path(__file__).parent / "static"
    return send_from_directory(static_dir, "index.html")


@app.route("/static/<path:path>")
def serve_static(path):
    """Serve static files."""
    static_dir = Path(__file__).parent / "static"
    return send_from_directory(static_dir, path)


@app.route("/api/chat", methods=["POST"])
def chat():
    """API endpoint to handle chat messages."""
    global chat_handler
    
    data = request.json
    user_message = data.get("message", "")
    crew_id = data.get("crew_id", None)
    logging.debug("Received chat message")

    if not user_message:
        logging.warning("No message provided in request")
        return jsonify({"error": "No message provided"}), 400

    try:
        # If a specific crew_id is provided, use that chat handler
        if crew_id and crew_id in chat_handlers:
            handler = chat_handlers[crew_id]
            # Update the global chat handler to track the currently active one
            chat_handler = handler
        elif chat_handler is None:
            return jsonify({"status": "error", "content": "No crew has been initialized. Please select a crew first."}), 400
        
        logging.debug("Processing message with chat_handler")
        response = chat_handler.process_message(user_message)
        
        # Ensure we have content in the response
        if not response.get("content") and response.get("status") == "success":
            logging.warning("Response content is empty despite successful status")
            response["content"] = "I'm sorry, but I couldn't generate a response. Please try again."
        
        # Include the active crew ID in the response
        response["crew_id"] = crew_id if crew_id else chat_handler.crew_name
        
        return jsonify(response)
    except Exception as e:
        error_message = f"Error processing chat message: {str(e)}"
        logging.error(error_message, exc_info=True)
        return jsonify({"status": "error", "content": error_message}), 500


@app.route("/api/initialize", methods=["GET", "POST"])
def initialize():
    """Initialize the chat handler and return initial message."""
    global chat_handler
    
    crew_id = None
    if request.method == "POST":
        # For POST, get crew_id from JSON body
        data = request.json or {}
        crew_id = data.get("crew_id")
    else:
        # For GET, get crew_id from query params
        crew_id = request.args.get("crew_id")
        
    try:
        # If crew_id is provided and valid, initialize that specific crew
        if crew_id:
            # If we already have this crew handler cached, use it
            if crew_id in chat_handlers:
                chat_handler = chat_handlers[crew_id]
            else:
                # Find the crew path from the discovered crews
                crew_path = None
                for crew in discovered_crews:
                    if crew.get("id") == crew_id:
                        crew_path = crew.get("path")
                        break
                        
                if not crew_path:
                    return jsonify({"status": "error", "message": f"Crew with ID {crew_id} not found"}), 404
                    
                # Load and initialize the specified crew
                crew_instance, crew_name = load_crew_from_module(Path(crew_path))
                new_handler = ChatHandler(crew_instance, crew_name)
                chat_handlers[crew_id] = new_handler
                chat_handler = new_handler
        
        # If no chat handler is set at this point, initialize with the default
        if not chat_handler:
            # Load and initialize the first available crew
            if discovered_crews:
                crew_path = discovered_crews[0].get("path")
                crew_instance, crew_name = load_crew_from_module(Path(crew_path))
                chat_handler = ChatHandler(crew_instance, crew_name)
                chat_handlers[discovered_crews[0].get("id")] = chat_handler
            else:
                # Fall back to the original behavior
                crew_instance, crew_name = load_crew()
                chat_handler = ChatHandler(crew_instance, crew_name)
                
        initial_message = chat_handler.initialize()
        return jsonify(
            {
                "status": "success",
                "message": initial_message,
                "required_inputs": [
                    {"name": field.name, "description": field.description}
                    for field in chat_handler.crew_chat_inputs.inputs
                ],
                "crew_id": crew_id or chat_handler.crew_name,
                "crew_name": chat_handler.crew_name,
                "crew_description": chat_handler.crew_chat_inputs.crew_description,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/crews", methods=["GET"])
def get_available_crews():
    """Get a list of all available crews."""
    return jsonify({
        "status": "success",
        "crews": discovered_crews
    })


def show_loading(stop_event, message):
    """Display animated loading dots while processing."""
    counter = 0
    while not stop_event.is_set():
        dots = "." * (counter % 4)
        click.echo(f"\r{message}{dots.ljust(3)}", nl=False)
        counter += 1
        threading.Event().wait(0.5)
    click.echo()  # Final newline


def main():
    """Main entry point for the CLI."""
    global chat_handler, discovered_crews

    click.echo("CrewAI Chat UI - Starting up...")

    try:
        # Try to discover all crews in the current directory
        click.echo("Discovering crews in current directory...")

        # Show loading indicator for crew loading
        stop_loading = threading.Event()
        loading_thread = threading.Thread(
            target=show_loading, args=(stop_loading, "Searching for crew files")
        )
        loading_thread.start()

        try:
            # Discover all available crews
            crews_info = discover_available_crews()
            
            # Add unique IDs to each crew
            for i, crew in enumerate(crews_info):
                crew["id"] = f"crew_{i}" if not crew.get("id") else crew["id"]
                
            discovered_crews = crews_info
            
            stop_loading.set()
            loading_thread.join()
            
            if crews_info:
                click.echo(f"Found {len(crews_info)} crews:")
                for i, crew in enumerate(crews_info):
                    click.echo(f"  {i+1}. {crew['name']} - {crew['directory']}")
                    
                # Initialize the first crew
                try:
                    crew_path = Path(crews_info[0]["path"])
                    crew, crew_name = load_crew_from_module(crew_path)
                    chat_handler = ChatHandler(crew, crew_name)
                    chat_handlers[crews_info[0]["id"]] = chat_handler
                    click.echo(f"Initialized {crew_name} as the default crew")
                except Exception as e:
                    click.echo(f"Error initializing first crew: {str(e)}", err=True)
            else:
                click.echo("No crews found. Trying fallback method...")
                try:
                    # Fallback to the original method
                    crew, crew_name = load_crew()
                    chat_handler = ChatHandler(crew, crew_name)
                    click.echo(f"Successfully loaded crew: {crew_name}")
                    
                    # Add this to discovered crews
                    discovered_crews = [{
                        "id": "default_crew",
                        "name": crew_name,
                        "path": str(Path(os.getcwd()) / "crew.py"),
                        "directory": "."
                    }]
                except Exception as e:
                    click.echo(f"Error loading crew: {str(e)}", err=True)
                    
                    # Add helpful debugging information
                    click.echo("\nFor debugging help:")
                    click.echo(
                        "1. Make sure your crew.py file contains a Crew instance or a function that returns one"
                    )
                    click.echo(
                        "2. If using a function, name it 'crew', 'get_crew', 'create_crew', or similar"
                    )
                    click.echo(
                        "3. Check that your CrewAI imports are correct for your installed version"
                    )
                    click.echo(
                        "4. Run your crew file directly with 'python crew.py' to test it"
                    )
                    sys.exit(1)
        except Exception as e:
            stop_loading.set()
            loading_thread.join()
            click.echo(f"Error discovering crews: {str(e)}", err=True)
            sys.exit(1)

        # Start the Flask server
        host = "0.0.0.0"  # Listen on all interfaces
        port = 3100

        click.echo(f"Starting web server at http://localhost:{port}")
        click.echo(f"Access the chat UI in your browser")
        click.echo("Press Ctrl+C to stop the server")

        app.run(host=host, port=port, debug=False)

    except KeyboardInterrupt:
        click.echo("\nServer stopped")
    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

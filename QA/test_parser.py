# test_parser.py
from AD_SignUp1 import parse_slack_message_with_langchain

def test_parsing():
    test_inputs = [
        "John Doe, Senior DevOps Engineer, Platform Team",
        "Sarah Chen as Frontend Developer for Mobile",
        "New hire Mike in Sales",
    ]
    
    for text in test_inputs:
        print(f"Input: {text}")
        result = parse_slack_message_with_langchain(text)
        print(f"Parsed: {result}\n")

test_parsing()
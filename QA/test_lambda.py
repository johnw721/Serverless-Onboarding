# test_lambda.py
import json
from AD_SignUp1 import lambda_handler

with open('test_event.json') as f:
    event = json.load(f)

result = lambda_handler(event, None)
print(json.dumps(result, indent=2))
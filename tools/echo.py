"""
description: Echo back the provided arguments
sensitivity: low
group: test
parameters:
    message:
        type: string
        description: Message to echo back
        required: true
"""


def handle(arguments, credentials):
    return {"echo": arguments}

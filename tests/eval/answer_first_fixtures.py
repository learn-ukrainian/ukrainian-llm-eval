"""Wire fixtures only; saved run answers must remain flat."""


def wire_responses(answers):
    return {"responses": {item_id: {"answer": answer, "explanation": "Fixture evidence."}
                          for item_id, answer in answers.items()}}

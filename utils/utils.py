from fastapi import status, HTTPException
import inspect

def getResponse(done: bool, errorMessage: str):
    if not done:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=errorMessage)
    return None


async def riseHttpExceptionIfNotFound(result, message: str):
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

async def await_if_coro(val):
    if inspect.iscoroutine(val):
        return await val
    return val
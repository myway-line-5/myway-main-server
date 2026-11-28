import uvicorn

if __name__ == "__main__":
    # 쉬는 연결 유지 시간 75초 (uvicorn 기본 5초).
    # 앱(OkHttp)이 쉬는 연결을 다시 쓰는 시간보다 서버가 더 오래 유지해야
    # "잠깐 쉬었다가 보낸 첫 요청"이 이미 닫힌 연결로 가서 실패하지 않는다.
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, timeout_keep_alive=75)

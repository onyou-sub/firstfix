"""관리자 비밀번호의 bcrypt 해시를 만든다 (.streamlit/secrets.toml의 password_hash에 붙여 넣기).

실행: uv run python scripts/make_password_hash.py
      (비밀번호는 화면에 보이지 않게 두 번 입력)
"""

import getpass
import sys

import bcrypt


def main() -> None:
    password = getpass.getpass("새 비밀번호: ")
    if len(password) < 8:
        sys.exit("비밀번호는 8자 이상이어야 합니다.")
    if getpass.getpass("한 번 더 입력: ") != password:
        sys.exit("두 비밀번호가 다릅니다.")
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    print("\n.streamlit/secrets.toml에 아래 줄을 넣으세요:\n")
    print(f'password_hash = "{hashed}"')


if __name__ == "__main__":
    main()

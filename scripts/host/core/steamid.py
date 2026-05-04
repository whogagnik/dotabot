from steam.client import SteamClient
from steam.enums import EResult

STEAMID64_BASE = 76561197960265728

def get_steamid3(username: str, password: str):
    steamid64 = get_steamid64(username, password)
    return steamid64_to_steamid3(steamid64)

def steamid64_to_steamid3(steamid64: str | int) -> str:
    steamid64 = int(steamid64)
    account_id = steamid64 - STEAMID64_BASE

    if account_id <= 0:
        raise ValueError("Некорректный steamid64")
    print(account_id)
    return str(account_id)
def get_steamid64(username: str, password: str) -> str:
    client = SteamClient()

    result = client.cli_login(username=username, password=password)

    if result != EResult.OK:
        client.logout()
        raise RuntimeError(f"Login failed: {result}")

    steamid64 = str(int(client.steam_id))

    client.logout()

    return steamid64


if __name__ == "__main__":
    login = input("Steam login: ").strip()
    password = input("Steam password: ").strip()

    print(get_steamid64(login, password))
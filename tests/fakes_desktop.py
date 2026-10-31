"""Bản giả của keyring và winreg cho test app desktop (không đụng Credential Manager / registry thật)."""


class FakeKeyring:
    """API như module keyring."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)  # keyring ném PasswordDeleteError
        del self.store[(service, name)]


class _Key:
    def __init__(self):
        self.subkeys: dict[str, "_Key"] = {}
        self.values: dict[str, object] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Đủ các hàm của winreg mà app dùng. Khoá lưu dạng cây trong bộ nhớ."""

    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.HKEY_CURRENT_USER = _Key()
        self.HKEY_LOCAL_MACHINE = _Key()

    def _walk(self, key, path, create=False):
        for part in [p for p in path.split("\\") if p]:
            if part not in key.subkeys:
                if not create:
                    raise FileNotFoundError(path)
                key.subkeys[part] = _Key()
            key = key.subkeys[part]
        return key

    def OpenKey(self, key, path, reserved=0, access=0):
        return self._walk(key, path)

    def CreateKey(self, key, path):
        return self._walk(key, path, create=True)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]

    def EnumKey(self, key, index):
        names = list(key.subkeys)
        if index >= len(names):
            raise OSError("no more keys")
        return names[index]

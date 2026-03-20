from datetime import datetime


class ConsoleLogger:
    PREFIX_MAP = {
        "INFO": "[*]",
        "OK": "[OK]",
        "WARN": "[!]",
        "ERROR": "[ERROR]",
        "DRY": "[DRY-RUN]",
        "DELETE": "[DELETE]",
        "ENABLE": "[ENABLED]",
        "DISABLE": "[DISABLED]",
        "REFRESH": "[REFRESH]",
        "SKIP": "[SKIP]",
    }

    def log(self, level: str, message: str, indent: int = 0) -> None:
        prefix = self.PREFIX_MAP.get(level, f"[{level}]")
        print(f"{'    ' * indent}{prefix} {message}")

    def token_header(self, idx: int, total: int, name: str) -> None:
        print(f"[{idx}/{total}] {name}")

    def banner(self, title: str) -> None:
        print("=" * 60)
        self.log("INFO", title)
        self.log("INFO", f"time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    def divider(self) -> None:
        print("=" * 60)

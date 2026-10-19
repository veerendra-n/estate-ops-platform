"""A simulated customer estate for the lab and demos: Contoso Retail and Fabrikam Health at the size of the
reference customer (about 300 servers, 3,000 users and devices, 50 applications).

Everything is generated from a fixed seed, so every run has the same estate. The state is plain data that
the simulator's vendor APIs read and the platform's skills change (moving a VM, freeing backup space), and
metric values are computed from it, so a fix shows up in the telemetry the same way it would in a real
estate. Faults are switched on by name and change that state the way the real failure would.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

GB = 1024**3
TB = 1024**4


@dataclass
class VM:
    name: str
    role: str
    host: str
    datastore: str
    os: str
    vcpus: int
    memory_mb: int
    disk_gb: int
    disk_used: float  # share of C: in use
    io_load: float  # IOPS it normally drives
    cpu: float
    memory: float
    services: dict[str, str] = field(default_factory=lambda: {"Spooler": "running", "W32Time": "running"})
    certificates: list[dict[str, Any]] = field(default_factory=list)
    power: str = "POWERED_ON"
    id: str = ""


@dataclass
class Datastore:
    name: str
    capacity_gb: int
    used_gb: int
    iops_capacity: float
    base_latency: float
    id: str = ""


@dataclass
class Job:
    name: str
    vms: list[str]
    repository: str
    size_gb: int
    sessions: list[dict[str, Any]] = field(default_factory=list)
    id: str = ""


@dataclass
class Backup:
    id: str
    name: str
    job_id: str | None  # None: the job was deleted, so these restore points are orphaned
    repository: str
    size_gb: int


@dataclass
class Estate:
    tenant: str
    name: str
    domain: str
    entra_tenant_id: str
    services: dict[str, dict[str, Any]]
    clusters: dict[str, list[str]]
    hosts: dict[str, dict[str, Any]]
    datastores: dict[str, Datastore]
    vms: dict[str, VM]
    repositories: dict[str, dict[str, Any]]
    jobs: dict[str, Job]
    backups: dict[str, Backup]
    network: dict[str, dict[str, Any]]
    devices: dict[str, dict[str, Any]]
    users: dict[str, dict[str, Any]]
    service_issues: list[dict[str, Any]] = field(default_factory=list)
    faults: dict[str, float] = field(default_factory=dict)  # name -> started at
    events: list[dict[str, Any]] = field(default_factory=list)  # what skills did, for the demo panel

    # ---- derived values (what the telemetry shows) ---------------------------------------------------

    def datastore_latency(self, ds: str) -> float:
        d = self.datastores[ds]
        load = sum(v.io_load for v in self.vms.values() if v.datastore == ds and v.power == "POWERED_ON")
        pressure = load / d.iops_capacity
        return round(d.base_latency * (1 + 9 * pressure**3), 2)

    def vm_disk_latency(self, vm: VM) -> float:
        return round(self.datastore_latency(vm.datastore) * 1.15 + 0.4, 2)

    def service_latency(self, service: str) -> float:
        s = self.services[service]
        worst = max((self.vm_disk_latency(self.vms[n]) for n in s["vms"] if n in self.vms), default=2.0)
        return round(s["base_latency_ms"] * (1 + max(0.0, worst - 6) / 4), 1)

    def service_errors(self, service: str) -> float:
        latency, base = self.service_latency(service), self.services[service]["base_latency_ms"]
        return round(min(0.25, 0.002 + max(0.0, latency / base - 3) * 0.004), 4)

    def repository_free(self, repo: str) -> float:
        r = self.repositories[repo]
        used = r["other_gb"] + sum(b.size_gb for b in self.backups.values() if b.repository == repo)
        return max(0.0, round(1 - used / r["capacity_gb"], 4))

    def repository_free_gb(self, repo: str) -> int:
        return int(self.repository_free(repo) * self.repositories[repo]["capacity_gb"])

    def event(self, what: str, **detail: Any) -> None:
        self.events.append({"at": time.time(), "what": what, **detail})
        del self.events[:-50]


def _rng(tenant: str) -> random.Random:
    return random.Random(f"estate-sim-{tenant}")  # noqa: S311 - simulation, not security


APP_NAMES = [
    "Intranet",
    "HR portal",
    "Timesheets",
    "Expenses",
    "Document management",
    "CRM",
    "Contract register",
    "Print management",
    "Visitor booking",
    "Fleet tracking",
    "Asset register",
    "Procurement",
    "Learning portal",
    "Quality management",
    "Service catalogue",
    "Reporting",
    "Data warehouse",
    "Integration bus",
    "Mail relay",
    "License server",
    "Antivirus console",
    "Monitoring proxy",
    "Jump hosts",
    "Build agents",
    "Test environment",
    "Archive",
    "Scanning",
    "Facilities",
    "Telephony",
    "Wi-Fi controller",
    "Badge system",
    "Kiosk management",
    "Survey tool",
    "Knowledge base",
    "Ticket archive",
    "PDF services",
    "Translation",
    "GIS",
    "Pricing engine",
    "Loyalty",
    "Payments gateway",
    "Recruitment",
]


def _build(
    tenant: str,
    name: str,
    prefix: str,
    domain: str,
    entra: str,
    core: list[dict[str, Any]],
    jobs_spec: list[tuple[str, list[str], int]],
    orphans: list[tuple[str, int]],
    repo_capacity_tb: int,
    repo_other_tb: float,
) -> Estate:
    rng = _rng(tenant)
    clusters = {
        f"{prefix}-PROD": [f"{prefix}-ESX0{i}" for i in range(1, 7)],
        f"{prefix}-NONPROD": [f"{prefix}-ESX07", f"{prefix}-ESX08"],
    }
    hosts = {}
    for cluster, members in clusters.items():
        for h in members:
            vendor = rng.choice(["Dell PowerEdge R760", "HPE ProLiant DL380 Gen11", "Lenovo ThinkSystem SR650 V3"])
            hosts[h] = {"name": h, "cluster": cluster, "model": vendor, "version": "8.0.3", "cpu": 0.0}
    datastores = {
        f"{prefix}-DS-CRIT-01": Datastore(f"{prefix}-DS-CRIT-01", 16384, 9100, 30000, 1.6),
        f"{prefix}-DS-CRIT-02": Datastore(f"{prefix}-DS-CRIT-02", 16384, 10200, 30000, 1.7),
        f"{prefix}-DS-GEN-01": Datastore(f"{prefix}-DS-GEN-01", 32768, 17000, 40000, 2.1),
        f"{prefix}-DS-GEN-02": Datastore(f"{prefix}-DS-GEN-02", 32768, 12500, 60000, 1.9),
        f"{prefix}-DS-NONPROD-01": Datastore(f"{prefix}-DS-NONPROD-01", 16384, 8000, 45000, 2.6),
    }
    prod_hosts = clusters[f"{prefix}-PROD"]
    vms: dict[str, VM] = {}
    services: dict[str, dict[str, Any]] = {}

    def add_vm(vm_name: str, role: str, ds: str, io: float, os_name: str = "Windows Server 2022", hosts_pool=prod_hosts) -> None:
        vms[vm_name] = VM(
            name=vm_name,
            role=role,
            host=hosts_pool[len(vms) % len(hosts_pool)],
            datastore=ds,
            os=os_name,
            vcpus=rng.choice([2, 4, 4, 8]),
            memory_mb=rng.choice([8192, 16384, 16384, 32768]),
            disk_gb=rng.choice([80, 120, 200]),
            disk_used=round(rng.uniform(0.35, 0.8), 3),
            io_load=io,
            cpu=round(rng.uniform(0.08, 0.45), 3),
            memory=round(rng.uniform(0.3, 0.75), 3),
        )

    for svc in core:
        services[svc["name"]] = {
            "name": svc["name"],
            "critical": svc.get("critical", False),
            "users": svc.get("users", 50),
            "owner": svc.get("owner", "IT"),
            "base_latency_ms": svc.get("latency", 220),
            "vms": [],
            "kind": svc.get("kind", "custom"),
        }
        for vm_name, role, ds, io, *os_name in svc["vms"]:
            add_vm(f"{prefix}-{vm_name}", role, f"{prefix}-{ds}", io, *(os_name or []))
            services[svc["name"]]["vms"].append(f"{prefix}-{vm_name}")
    # The other applications, one to five servers each, to the reference customer's size.
    n = 1
    for app in APP_NAMES:
        count = rng.randint(1, 5)
        names = []
        for _ in range(count):
            role = rng.choice(["app", "app", "web", "sql"])
            os_name = rng.choice(
                ["Windows Server 2022", "Windows Server 2022", "Windows Server 2019", "Ubuntu 22.04", "Windows Server 2016"]
            )
            vm_name = f"{prefix}-APP{n:03d}"
            ds = rng.choice([f"{prefix}-DS-GEN-01", f"{prefix}-DS-GEN-02"])
            add_vm(vm_name, role, ds, rng.uniform(80, 400), os_name)
            names.append(vm_name)
            n += 1
        services[app] = {
            "name": app,
            "critical": False,
            "users": rng.randint(20, 900),
            "owner": "IT",
            "base_latency_ms": 300,
            "vms": names,
            "kind": "cots",
        }
    while len(vms) < 300:
        add_vm(
            f"{prefix}-TST{n:03d}",
            "test",
            f"{prefix}-DS-NONPROD-01",
            rng.uniform(30, 150),
            "Windows Server 2022",
            clusters[f"{prefix}-NONPROD"],
        )
        n += 1
    for i, vm in enumerate(vms.values()):
        vm.id = f"vm-{1000 + i}"
        if vm.role == "web":
            vm.certificates.append(
                {"subject": f"{vm.name.lower()}.{domain}", "days_left": rng.randint(120, 380), "thumbprint": f"{i:08X}"}
            )
    for i, ds in enumerate(datastores.values()):
        ds.id = f"datastore-{200 + i}"

    repo = f"{prefix}-REPO-01"
    repositories = {
        repo: {
            "id": "88788f9e-d8f5-4eb4-bc4f-9b3f5403bcec",
            "name": repo,
            "capacity_gb": repo_capacity_tb * 1024,
            "other_gb": int(repo_other_tb * 1024),
        }
    }
    jobs, backups = {}, {}
    for i, (job_name, members, size) in enumerate(jobs_spec):
        jid = f"0b5c9b0e-{i:04d}-4f1e-9a51-6f0d2c9e{i:04d}"
        job = Job(job_name, [f"{prefix}-{m}" for m in members], repo, size, id=jid)
        job.sessions.append(_session(job, "Success", time.time() - 6 * 3600, "Job finished successfully"))
        jobs[job_name] = job
        backups[f"bk-{i}"] = Backup(f"bk-{i}", job_name, jid, repo, size * 6)
    for j, (old, size) in enumerate(orphans):
        backups[f"orphan-{j}"] = Backup(f"orphan-{j}", f"{prefix}-{old} (job deleted)", None, repo, size)

    network = {
        f"{prefix}-CORE-01": {
            "name": f"{prefix}-CORE-01",
            "kind": "switch",
            "model": "Cisco Catalyst 9500-48Y4C",
            "version": "17.9.5",
            "site": "HQ",
        }
    }
    for i in range(1, 13):
        network[f"{prefix}-ACC-{i:02d}"] = {
            "name": f"{prefix}-ACC-{i:02d}",
            "kind": "switch",
            "model": "Cisco Catalyst 9300-48P",
            "version": "17.6.4",
            "site": "HQ",
        }
    network[f"{prefix}-FW-01"] = {
        "name": f"{prefix}-FW-01",
        "kind": "firewall",
        "model": "FortiGate 100F",
        "version": "7.2.8",
        "site": "HQ",
    }

    users, devices = {}, {}
    first = ["priya", "arun", "mei", "sam", "lena", "omar", "jade", "tom", "nina", "raj", "ella", "luca", "sara", "ben", "ivy"]
    last = [
        "nair",
        "patel",
        "chen",
        "okafor",
        "berg",
        "haddad",
        "lee",
        "price",
        "kowal",
        "rao",
        "smith",
        "rossi",
        "lind",
        "ward",
        "moss",
    ]
    for i in range(1, 3001):
        if i == 231:
            given, family = "priya", "nair"
        else:
            given, family = rng.choice(first), rng.choice(last)
        upn = f"{given}.{family}{'' if i == 231 else i}@{domain}"
        users[upn] = {
            "id": f"u-{tenant}-{i:04d}",
            "displayName": f"{given.title()} {family.title()}",
            "userPrincipalName": upn,
            "accountEnabled": True,
            "mail": upn,
            "jobTitle": rng.choice(["Analyst", "Store manager", "Nurse", "Engineer", "Accountant", "Clerk", "Buyer"]),
            "locked_out": False,
        }
        mac = rng.random() < 0.1
        dname = f"LT-{prefix}-{i:04d}"
        devices[dname] = {
            "id": f"d-{tenant}-{i:04d}",
            "deviceName": dname,
            "operatingSystem": "macOS" if mac else "Windows",
            "osVersion": "15.4" if mac else rng.choice(["10.0.26100.4061", "10.0.26100.4061", "10.0.22631.5335"]),
            "complianceState": "compliant" if rng.random() > 0.04 else "noncompliant",
            "lastSyncDateTime": _iso(time.time() - rng.randint(600, 86400)),
            "userPrincipalName": upn,
            "serialNumber": f"SN{rng.randint(10**7, 10**8 - 1)}",
            "manufacturer": "Apple" if mac else rng.choice(["Dell Inc.", "HP", "Lenovo"]),
            "model": "MacBook Air" if mac else rng.choice(["Latitude 7450", "EliteBook 840 G11", "ThinkPad T14 Gen 5"]),
            "managementAgent": "mdm",
            "vpn_cert_days_left": rng.randint(60, 300),
            "vpn_drops": 0,
        }
    return Estate(
        tenant=tenant,
        name=name,
        domain=domain,
        entra_tenant_id=entra,
        services=services,
        clusters=clusters,
        hosts=hosts,
        datastores=datastores,
        vms=vms,
        repositories=repositories,
        jobs=jobs,
        backups=backups,
        network=network,
        devices=devices,
        users=users,
    )


def _session(job: Job, result: str, ended: float, message: str, state: str = "Stopped") -> dict[str, Any]:
    started = ended - 1800
    return {
        "id": f"{job.id[:8]}-{int(ended)}",
        "name": job.name,
        "jobId": job.id,
        "sessionType": "BackupJob",
        "state": state,
        "creationTime": _iso(started),
        "endTime": _iso(ended) if state == "Stopped" else None,
        "result": {"result": result, "message": message, "isCanceled": False},
    }


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def contoso() -> Estate:
    return _build(
        "contoso",
        "Contoso Retail",
        "CTO",
        "contoso.example",
        "9f4c2a1e-1111-4c3b-9d1a-c0a5e0000001",
        core=[
            {
                "name": "Checkout",
                "critical": True,
                "users": 1800,
                "owner": "Digital",
                "latency": 240,
                "vms": [
                    ("SHOP-WEB01", "web", "DS-CRIT-02", 300),
                    ("SHOP-WEB02", "web", "DS-CRIT-02", 300),
                    ("SHOP-WEB03", "web", "DS-CRIT-02", 300),
                    ("SHOP-WEB04", "web", "DS-CRIT-02", 300),
                    ("SHOP-API01", "app", "DS-CRIT-02", 900),
                    ("SHOP-API02", "app", "DS-CRIT-02", 900),
                    ("SHOP-API03", "app", "DS-CRIT-02", 900),
                    ("SHOP-SQL01", "sql", "DS-CRIT-02", 4200),
                ],
            },
            {
                "name": "Online shop",
                "critical": True,
                "users": 1800,
                "owner": "Digital",
                "latency": 300,
                "vms": [
                    ("SHOP-CMS01", "web", "DS-CRIT-02", 400),
                    ("SHOP-CMS02", "web", "DS-CRIT-02", 400),
                    ("SHOP-SEARCH01", "app", "DS-CRIT-02", 1200),
                ],
            },
            {
                "name": "Store POS",
                "critical": True,
                "users": 600,
                "owner": "Retail ops",
                "latency": 180,
                "vms": [
                    ("POS-APP01", "app", "DS-CRIT-01", 600),
                    ("POS-APP02", "app", "DS-CRIT-01", 600),
                    ("POS-SQL01", "sql", "DS-CRIT-01", 2600),
                ],
            },
            {
                "name": "Finance ERP",
                "critical": True,
                "users": 420,
                "owner": "Finance",
                "latency": 350,
                "vms": [
                    ("ERP-APP01", "app", "DS-CRIT-01", 700),
                    ("ERP-APP02", "app", "DS-CRIT-01", 700),
                    ("ERP-SQL01", "sql", "DS-CRIT-01", 3800),
                ],
            },
            {
                "name": "File shares",
                "users": 2600,
                "latency": 120,
                "vms": [("FS01", "file", "DS-GEN-01", 1500), ("FS02", "file", "DS-GEN-01", 1500)],
            },
            {
                "name": "Identity",
                "critical": True,
                "users": 3000,
                "latency": 40,
                "vms": [("DC01", "dc", "DS-CRIT-01", 200), ("DC02", "dc", "DS-CRIT-01", 200)],
            },
            # BI shares the shop's datastore: the noisy neighbour in the checkout scenario.
            {
                "name": "Sales analytics",
                "users": 40,
                "owner": "Finance",
                "latency": 900,
                "vms": [("BI-ETL01", "etl", "DS-CRIT-02", 500), ("BI-SQL01", "sql", "DS-GEN-02", 1500)],
            },
        ],
        jobs_spec=[
            ("Shop SQL nightly", ["SHOP-SQL01"], 420),
            ("ERP nightly", ["ERP-SQL01", "ERP-APP01", "ERP-APP02"], 610),
            ("File servers", ["FS01", "FS02"], 1800),
            ("Infrastructure", ["DC01", "DC02"], 120),
        ],
        orphans=[("OLD-WMS01", 380)],
        repo_capacity_tb=60,
        repo_other_tb=8,
    )


def fabrikam() -> Estate:
    return _build(
        "fabrikam",
        "Fabrikam Health",
        "FAB",
        "fabrikam.example",
        "1b7d3e5f-2222-4a6b-8c9d-f0a5e0000002",
        core=[
            {
                "name": "EHR",
                "critical": True,
                "users": 2200,
                "owner": "Clinical systems",
                "latency": 380,
                "vms": [
                    ("EHR-WEB01", "web", "DS-CRIT-01", 400),
                    ("EHR-WEB02", "web", "DS-CRIT-01", 400),
                    ("EHR-APP01", "app", "DS-CRIT-01", 1100),
                    ("EHR-APP02", "app", "DS-CRIT-01", 1100),
                    ("EHR-SQL01", "sql", "DS-CRIT-01", 5200),
                ],
            },
            {
                "name": "PACS imaging",
                "critical": True,
                "users": 300,
                "owner": "Radiology",
                "latency": 600,
                "vms": [("PACS-APP01", "app", "DS-CRIT-02", 2400), ("PACS-DB01", "sql", "DS-CRIT-02", 3000)],
            },
            {
                "name": "Patient portal",
                "users": 9000,
                "owner": "Digital",
                "latency": 260,
                "vms": [("PORTAL-WEB01", "web", "DS-GEN-02", 300), ("PORTAL-WEB02", "web", "DS-GEN-02", 300)],
            },
            {
                "name": "Payroll",
                "users": 35,
                "owner": "HR",
                "latency": 400,
                "vms": [("PAY-APP01", "app", "DS-GEN-01", 300), ("PAY-DB01", "sql", "DS-GEN-01", 900)],
            },
            {
                "name": "Identity",
                "critical": True,
                "users": 3000,
                "latency": 40,
                "vms": [("DC01", "dc", "DS-CRIT-01", 200), ("DC02", "dc", "DS-CRIT-01", 200)],
            },
            {"name": "File shares", "users": 2500, "latency": 120, "vms": [("FS01", "file", "DS-GEN-01", 1500)]},
        ],
        jobs_spec=[
            ("EHR SQL nightly", ["EHR-SQL01"], 880),
            ("EHR application", ["EHR-APP01", "EHR-APP02", "EHR-WEB01", "EHR-WEB02"], 300),
            ("PACS", ["PACS-DB01", "PACS-APP01"], 1400),
            ("Business systems", ["PAY-DB01", "PAY-APP01", "FS01"], 650),
        ],
        # Restore points of servers decommissioned last year whose job was deleted but not the backups.
        orphans=[("OLD-LIS01", 720), ("OLD-LIS02", 690)],
        repo_capacity_tb=24,
        repo_other_tb=1,
    )


def build_all() -> dict[str, Estate]:
    return {"contoso": contoso(), "fabrikam": fabrikam()}


# ---- faults ----------------------------------------------------------------------------------------------

FAULTS: dict[str, dict[str, str]] = {
    "checkout-slow": {
        "tenant": "contoso",
        "title": "Checkout slows down (scenario 1)",
        "what": "A runaway ETL job on CTO-BI-ETL01 saturates datastore CTO-DS-CRIT-02, which Checkout and Online shop run on.",
    },
    "backup-repo-full": {
        "tenant": "fabrikam",
        "title": "Nightly backup fails, repository full (scenario 2)",
        "what": "FAB-REPO-01 fills up and the EHR SQL nightly job fails for lack of space.",
    },
    "vpn-cert-expired": {
        "tenant": "contoso",
        "title": "A user's VPN keeps dropping (scenario 3)",
        "what": "The VPN certificate on Priya Nair's laptop LT-CTO-0231 has expired.",
    },
    "cert-and-disk-forecast": {
        "tenant": "contoso",
        "title": "A certificate expires in 9 days and a disk fills in 6 (scenario 4)",
        "what": "shop.contoso.example on CTO-SHOP-WEB01 expires in 9 days; CTO-FS01 C: is filling.",
    },
    "spooler-stopped": {
        "tenant": "fabrikam",
        "title": "Print spooler stopped on a server",
        "what": "The Print Spooler service stops on FAB-APP003.",
    },
}


def inject(estate: Estate, fault: str) -> None:
    if FAULTS[fault]["tenant"] != estate.tenant:
        raise ValueError(f"{fault} is a {FAULTS[fault]['tenant']} fault")
    estate.faults[fault] = time.time()
    if fault == "checkout-slow":
        estate.vms["CTO-BI-ETL01"].io_load = 30000
        estate.vms["CTO-BI-ETL01"].cpu = 0.92
    elif fault == "backup-repo-full":
        repo = estate.repositories["FAB-REPO-01"]
        used = sum(b.size_gb for b in estate.backups.values() if b.repository == "FAB-REPO-01")
        repo["other_gb"] = repo["capacity_gb"] - used - 90  # 90 GB left
        job = estate.jobs["EHR SQL nightly"]
        job.sessions.append(
            _session(
                job, "Failed", time.time() - 120, "Insufficient free space on repository FAB-REPO-01: 90 GB free, 880 GB required"
            )
        )
    elif fault == "vpn-cert-expired":
        estate.devices["LT-CTO-0231"]["vpn_cert_days_left"] = -2
        estate.devices["LT-CTO-0231"]["vpn_drops"] = 14
    elif fault == "cert-and-disk-forecast":
        estate.vms["CTO-SHOP-WEB01"].certificates[0]["days_left"] = 9
        estate.vms["CTO-FS01"].disk_used = 0.83
    elif fault == "spooler-stopped":
        estate.vms["FAB-APP003"].services["Spooler"] = "stopped"


def disk_used_now(estate: Estate, vm: VM) -> float:
    """CTO-FS01 fills steadily while the forecast fault is on (to 100% in about 6 days)."""
    started = estate.faults.get("cert-and-disk-forecast")
    if started and vm.name == "CTO-FS01" and vm.disk_used >= 0.8:
        return min(1.0, vm.disk_used + (time.time() - started) / (6 * 86400) * 0.17)
    return vm.disk_used


def noise(key: str, scale: float) -> float:
    """Small, smooth wobble so graphs look alive without random jumps between scrapes."""
    t = time.time() / 60
    h = (hash(key) % 1000) / 1000
    return scale * (math.sin(t + h * 6.28) * 0.6 + math.sin(t * 2.7 + h * 3.1) * 0.4)

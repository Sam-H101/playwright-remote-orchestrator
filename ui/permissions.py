"""What people may do on the Grid UI: its fixed permissions and the built-in roles made of them.

A role is a set of permissions, and someone holds every permission of every role they have, plus what those
imply (mfa.all brings mfa.add, which brings mfa.use). The built-in roles live here rather than in the database,
so nobody can change them, and they have fixed negative ids so they never meet a custom role's. Custom roles keep
their permissions in roles.permissions, space separated. Nothing here touches the database or Flask: accounts.py
works out who has which roles, and auth.py checks what they may do.
"""
import collections

# id -> (group, label, description), in the order the role editor shows them. The ids are part of the API.
PERMISSIONS = collections.OrderedDict([
    ("grid.view", ("Grid", "See the grid",
                   "Open the overview, activity and sessions when the grid needs sign-in to view them "
                   "(GRID_UI_READS).")),
    ("grid.operate", ("Grid", "Run the grid", "Release slots, reset failure counts and forget nodes.")),
    ("tokens.own", ("Access tokens", "Make client tokens",
                    "Make client tokens for their own checks, and see, revoke and delete their own. A check using "
                    "one acts as them. Making the grid's first client token, or deleting its last, needs “Every "
                    "access token”: the hub then starts or stops turning away checks without one.")),
    ("tokens.agents", ("Access tokens", "Add hosts",
                       "Make agent tokens (machines that join the grid), and see, revoke and delete every agent "
                       "token. A host sees everything the checks sent to it do, including the sign-ins and codes "
                       "they use: give this only to people you'd trust with those.")),
    ("tokens.all", ("Access tokens", "Every access token",
                    "See, revoke and delete everyone's tokens, and give an ownerless client token to a person.")),
    ("mfa.use", ("MFA secrets", "Use MFA secrets",
                 "See the MFA secrets they own or that are shared with them, show and use their codes, and let "
                 "their checks get codes.")),
    ("mfa.add", ("MFA secrets", "Add MFA secrets",
                 "Add MFA secrets (they own them), and share and delete their own.")),
    ("mfa.all", ("MFA secrets", "Every MFA secret",
                 "See, use, share, delete and change the owner of every MFA secret.")),
    ("signins.use", ("Saved sign-ins", "Use saved sign-ins",
                     "See the saved sign-ins they own or that are shared with them, and let their checks reuse "
                     "them. Only the owner's checks sign in fresh and save.")),
    ("signins.add", ("Saved sign-ins", "Add saved sign-ins",
                     "Let their checks start saved sign-ins under new names (they own them), and forget, share and "
                     "delete their own.")),
    ("signins.all", ("Saved sign-ins", "Every saved sign-in",
                     "See, forget, share, delete and change the owner of every saved sign-in. Their checks still "
                     "use only their own, Admins', and those shared with them or a role they have (for admins, "
                     "the Admin role).")),
    ("users.view", ("People", "See people and roles",
                    "See users, roles, group mappings and the single sign-on settings.")),
    ("users.manage", ("People", "Manage people and roles",
                      "Disable and enable users, end their sessions, revoke their tokens, give and take roles, "
                      "make, change and delete custom roles and map groups, but only with permissions they have "
                      "themselves.")),
    ("audit.view", ("People", "Read the audit log", "Read every recorded change.")),
])
# What holding a permission brings with it. expand() follows these all the way down.
IMPLIES = {
    "grid.operate": ("grid.view",),
    "tokens.all": ("tokens.own", "tokens.agents"),
    "mfa.add": ("mfa.use",),
    "mfa.all": ("mfa.add",),
    "signins.add": ("signins.use",),
    "signins.all": ("signins.add",),
    "users.manage": ("users.view",),
}
ALL = frozenset(PERMISSIONS)
# What anyone may do on a grid with no sign-in configured, as they always could.
OPEN_GRID = frozenset({"grid.view", "grid.operate"})

ADMIN, OPERATOR, TESTER, VIEWER = -1, -2, -3, -4
# id -> (name, description, permissions before expand()).
BUILTIN_ROLES = {
    ADMIN: ("Admin", "Everything, including people, roles and every secret.", ALL),
    OPERATOR: ("Operator", "Runs the grid and writes checks: grid actions, adding hosts (which see what checks do on "
                           "them), their own client tokens, and the MFA secrets and sign-ins they add or that are "
                           "shared with them.",
               frozenset({"grid.operate", "tokens.own", "tokens.agents", "mfa.add", "signins.add"})),
    TESTER: ("Tester", "Writes checks: their own client tokens, and the MFA secrets and sign-ins they add or that "
                       "are shared with them.", frozenset({"grid.view", "tokens.own", "mfa.add", "signins.add"})),
    VIEWER: ("Viewer", "Sees the grid and changes nothing. Matters only when the grid needs sign-in to view it.",
             frozenset({"grid.view"})),
}
# The owner shown for items nobody owns (made with the admin token, or before there were users), which a custom
# role may not be called either.
ADMINS_LABEL = "Admins"


def expand(perms):
    """The known permissions among perms, plus everything they imply."""
    found = set()
    pending = [p for p in perms if p in PERMISSIONS]
    while pending:
        perm = pending.pop()
        if perm not in found:
            found.add(perm)
            pending.extend(IMPLIES.get(perm, ()))
    return frozenset(found)


def parse(text):
    """The permissions a roles.permissions value names, without what they imply. Ids this version doesn't know
    (a newer version wrote them, or one was retired) are left out."""
    return frozenset(p for p in (text or "").split() if p in PERMISSIONS)


def unparse(perms):
    """The roles.permissions value for a set of permissions: sorted and space separated."""
    return " ".join(sorted(set(perms)))


def catalogue():
    """Every permission for the role editor, in order. implies is everything it brings with it, all the way down,
    so the page can tick and lock those without working it out."""
    return [{"id": perm, "group": group, "label": label, "description": description,
             "implies": [p for p in PERMISSIONS if p != perm and p in expand([perm])]}
            for perm, (group, label, description) in PERMISSIONS.items()]


def reserved_names():
    """Names a custom role can't take, casefolded: the built-in roles' and the owner label of ownerless items."""
    return {name.casefold() for name, _, _ in BUILTIN_ROLES.values()} | {ADMINS_LABEL.casefold()}

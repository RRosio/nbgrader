#!/usr/bin/env python
"""
Backport pull requests to a particular branch.

Usage: backport_pr.py branch [PR] [PR2]

e.g.:

    python tools/backport_pr.py 0.13.1 123 155

to backport PR #123 onto branch 0.13.1

or
    python tools/backport_pr.py 2.1

to see what PRs are marked for backport with milestone=2.1 that have yet to be applied
to branch 2.x.

Forked from the backport_pr.py script in the ipython/ipython repository.

"""

from __future__ import print_function

import os
import re
import sys
import requests
import json

from subprocess import Popen, PIPE, check_call, check_output
try:
    from urllib.request import urlopen
except ImportError:
    from urllib import urlopen


class Obj(dict):
    """Dictionary with attribute access to names."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, val):
        self[name] = val


def get_paged_request(url, headers=None, **params):
    """get a full list, handling APIv3's paging"""
    results = []
    params.setdefault("per_page", 100)
    while True:
        if '?' in url:
            params = None
            print("fetching %s" % url, file=sys.stderr)
        else:
            print("fetching %s with %s" % (url, params), file=sys.stderr)
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        results.extend(response.json())
        if 'next' in response.links:
            url = response.links['next']['url']
        else:
            break
    return results


def get_issues_list(project, auth=False, **params):
    """get issues list"""
    params.setdefault("state", "closed")
    url = "https://api.github.com/repos/{project}/issues".format(project=project)
    if auth:
        headers = make_auth_header()
    else:
        headers = None
    pages = get_paged_request(url, headers=headers, **params)
    return pages


def get_pull_request(project, num, auth=False):
    """get pull request info  by number
    """
    url = "https://api.github.com/repos/{project}/pulls/{num}".format(project=project, num=num)
    if auth:
        header = make_auth_header()
    else:
        header = None
    print("fetching %s" % url, file=sys.stderr)
    response = requests.get(url, headers=header)
    response.raise_for_status()
    return json.loads(response.text, object_hook=Obj)


def get_pull_request_files(project, num, auth=False):
    """get list of files in a pull request"""
    url = "https://api.github.com/repos/{project}/pulls/{num}/files".format(project=project, num=num)
    if auth:
        header = make_auth_header()
    else:
        header = None
    return get_paged_request(url, headers=header)


def is_pull_request(issue):
    """Return True if the given issue is a pull request."""
    return bool(issue.get('pull_request', {}).get('html_url', None))


def get_milestones(project, auth=False, **params):
    params.setdefault('state', 'all')
    url = "https://api.github.com/repos/{project}/milestones".format(project=project)
    if auth:
        headers = make_auth_header()
    else:
        headers = None
    milestones = get_paged_request(url, headers=headers, **params)
    return milestones

def get_milestone_id(project, milestone, auth=False, **params):
    milestones = get_milestones(project, auth=auth, **params)
    for mstone in milestones:
        if mstone['title'] == milestone:
            return mstone['number']
    else:
        raise ValueError("milestone %s not found" % milestone)


def find_rejects(root='.'):
    for dirname, dirs, files in os.walk(root):
        for fname in files:
            if fname.endswith('.rej'):
                yield os.path.join(dirname, fname)


def get_current_branch():
    branches = check_output(['git', 'branch'])
    for branch in branches.splitlines():
        if branch.startswith(b'*'):
            return branch[1:].strip().decode('utf-8')


def backport_pr(branch, num, project='jupyter/nbgrader'):
    current_branch = get_current_branch()
    if branch != current_branch:
        check_call(['git', 'checkout', branch])
    check_call(['git', 'pull'])
    pr = get_pull_request(project, num, auth=False)
    files = get_pull_request_files(project, num, auth=False)
    patch_url = pr['patch_url']
    title = pr['title']
    description = pr['body']
    fname = "PR%i.patch" % num
    if os.path.exists(fname):
        print("using patch from {fname}".format(**locals()))
        with open(fname, 'rb') as f:
            patch = f.read()
    else:
        req = urlopen(patch_url)
        patch = req.read()

    lines = description.splitlines()
    if len(lines) > 5:
        lines = lines[:5] + ['...']
        description = '\n'.join(lines)

    msg = "Backport PR #%i: %s" % (num, title) + '\n\n' + description
    check = Popen(['git', 'apply', '--check', '--verbose'], stdin=PIPE)
    check.communicate(patch)

    if check.returncode:
        print("patch did not apply, saving to {fname}".format(**locals()))
        print("edit {fname} until `cat {fname} | git apply --check` succeeds".format(**locals()))
        print("then run tools/backport_pr.py {num} again".format(**locals()))
        if not os.path.exists(fname):
            with open(fname, 'wb') as f:
                f.write(patch)
        return 1

    p = Popen(['git', 'apply'], stdin=PIPE)
    p.communicate(patch)

    filenames = [f['filename'] for f in files]

    check_call(['git', 'add'] + filenames)
    check_call(['git', 'commit', '-m', msg])

    print("PR #%i applied, with msg:" % num)
    print()
    print(msg)
    print()

    if branch != current_branch:
        check_call(['git', 'checkout', current_branch])

    return 0

backport_re = re.compile(r"(?:[Bb]ackport|[Mm]erge).*#(\d+)")

def already_backported(branch, since_tag=None):
    """return set of PRs that have been backported already"""
    if since_tag is None:
        since_tag = check_output(['git', 'describe', branch, '--abbrev=0', '--tags']).decode('utf8').strip()
    cmd = ['git', 'log', '%s..%s' % (since_tag, branch), '--oneline']
    lines = check_output(cmd).decode('utf8')
    return set(int(num) for num in backport_re.findall(lines))

def should_backport(labels=None, milestone=None):
    """return set of PRs marked for backport"""
    if labels is None and milestone is None:
        raise ValueError("Specify one of labels or milestone.")
    elif labels is not None and milestone is not None:
        raise ValueError("Specify only one of labels or milestone.")
    if labels is not None:
        issues = get_issues_list(
            "jupyter/nbgrader",
            labels=labels,
            state='closed',
            auth=False)
    else:
        milestone_id = get_milestone_id(
            "jupyter/nbgrader",
            milestone,
            auth=False)
        issues = get_issues_list(
            "jupyter/nbgrader",
            milestone=milestone_id,
            state='closed',
            auth=False)

    should_backport = set()
    for issue in issues:
        if not is_pull_request(issue):
            continue
        pr = get_pull_request(
            "jupyter/nbgrader",
            issue['number'],
            auth=False)
        if not pr['merged']:
            print ("Marked PR closed without merge: %i" % pr['number'])
            continue
        if pr['base']['ref'] != 'master':
            continue
        should_backport.add(pr['number'])
    return should_backport

if __name__ == '__main__':

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if len(sys.argv) < 3:
        milestone = sys.argv[1]
        branch = ".".join(milestone.split('.')[:-1]) + '.x'
        already = already_backported(branch)
        should = should_backport(milestone=milestone)
        print ("The following PRs should be backported:")
        for pr in sorted(should.difference(already)):
            print (pr)
        sys.exit(0)

    for prno in map(int, sys.argv[2:]):
        print("Backporting PR #%i" % prno)
        rc = backport_pr(sys.argv[1], prno)
        if rc:
            print("Backporting PR #%i failed" % prno)
            sys.exit(rc)

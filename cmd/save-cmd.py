#!/usr/bin/env python
import sys, re, errno, stat, time, math
from bup import hashsplit, git, options, index, client
from bup.helpers import *


optspec = """
bup save [-tc] [-n name] <filenames...>
--
r,remote=  remote repository path
t,tree     output a tree id
c,commit   output a commit id
n,name=    name of backup set to update (if any)
v,verbose  increase log output (can be used more than once)
q,quiet    don't show progress meter
smaller=   only back up files smaller than n bytes
"""
o = options.Options('bup save', optspec)
(opt, flags, extra) = o.parse(sys.argv[1:])

git.check_repo_or_die()
if not (opt.tree or opt.commit or opt.name):
    o.fatal("use one or more of -t, -c, -n")
if not extra:
    o.fatal("no filenames given")

opt.progress = (istty and not opt.quiet)

refname = opt.name and 'refs/heads/%s' % opt.name or None
if opt.remote:
    cli = client.Client(opt.remote)
    oldref = refname and cli.read_ref(refname) or None
    w = cli.new_packwriter()
else:
    cli = None
    oldref = refname and git.read_ref(refname) or None
    w = git.PackWriter()


def eatslash(dir):
    if dir.endswith('/'):
        return dir[:-1]
    else:
        return dir


parts = ['']
shalists = [[]]

def _push(part):
    assert(part)
    parts.append(part)
    shalists.append([])

def _pop(force_tree):
    assert(len(parts) >= 1)
    part = parts.pop()
    shalist = shalists.pop()
    tree = force_tree or w.new_tree(shalist)
    if shalists:
        shalists[-1].append(('40000', part, tree))
    else:  # this was the toplevel, so put it back for sanity
        shalists.append(shalist)
    return tree

lastremain = None
def progress_report(n):
    global count, subcount, lastremain
    subcount += n
    cc = count + subcount
    pct = total and (cc*100.0/total) or 0
    now = time.time()
    elapsed = now - tstart
    kps = elapsed and int(cc/1024./elapsed)
    kps_frac = 10 ** int(math.log(kps+1, 10) - 1)
    kps = int(kps/kps_frac)*kps_frac
    if cc:
        remain = elapsed*1.0/cc * (total-cc)
    else:
        remain = 0.0
    if (lastremain and (remain > lastremain)
          and ((remain - lastremain)/lastremain < 0.05)):
        remain = lastremain
    else:
        lastremain = remain
    hours = int(remain/60/60)
    mins = int(remain/60 - hours*60)
    secs = int(remain - hours*60*60 - mins*60)
    if elapsed < 30:
        remainstr = ''
        kpsstr = ''
    else:
        kpsstr = '%dk/s' % kps
        if hours:
            remainstr = '%dh%dm' % (hours, mins)
        elif mins:
            remainstr = '%dm%d' % (mins, secs)
        else:
            remainstr = '%ds' % secs
    progress('Saving: %.2f%% (%d/%dk, %d/%d files) %s %s\r'
             % (pct, cc/1024, total/1024, fcount, ftotal,
                remainstr, kpsstr))


r = index.Reader(git.repo('bupindex'))

def already_saved(ent):
    return ent.is_valid() and w.exists(ent.sha) and ent.sha

def wantrecurse_pre(ent):
    return not already_saved(ent)

def wantrecurse_during(ent):
    return not already_saved(ent) or ent.sha_missing()

total = ftotal = 0
if opt.progress or 1:
    for (transname,ent) in r.filter(extra, wantrecurse=wantrecurse_pre):
        if not (ftotal % 10024):
            progress('Reading index: %d\r' % ftotal)
        exists = ent.exists()
        hashvalid = already_saved(ent)
        ent.set_sha_missing(not hashvalid)
        if exists and not hashvalid:
            total += ent.size
        ftotal += 1
    progress('Reading index: %d, done.\n' % ftotal)
    hashsplit.progress_callback = progress_report

tstart = time.time()
count = subcount = fcount = 0
for (transname,ent) in r.filter(extra, wantrecurse=wantrecurse_during):
    (dir, file) = os.path.split(ent.name)
    exists = (ent.flags & index.IX_EXISTS)
    hashvalid = already_saved(ent)
    oldsize = ent.size
    if opt.verbose:
        if not exists:
            status = 'D'
        elif not hashvalid:
            if ent.sha == index.EMPTY_SHA:
                status = 'A'
            else:
                status = 'M'
        else:
            status = ' '
        if opt.verbose >= 2 or stat.S_ISDIR(ent.mode):
            log('%s %-70s\n' % (status, ent.name))

    if opt.progress:
        progress_report(0)
    fcount += 1
    
    if not exists:
        continue

    assert(dir.startswith('/'))
    dirp = dir.split('/')
    while parts > dirp:
        _pop(force_tree = None)
    if dir != '/':
        for part in dirp[len(parts):]:
            _push(part)

    if not file:
        # sub/parentdirectories already handled in the pop/push() part above.
        oldtree = already_saved(ent) # may be None
        newtree = _pop(force_tree = oldtree)
        if not oldtree:
            ent.validate(040000, newtree)
            ent.repack()
        if exists and ent.sha_missing():
            count += oldsize
        continue

    # it's not a directory
    id = None
    if hashvalid:
        mode = '%o' % ent.gitmode
        id = ent.sha
        shalists[-1].append((mode, file, id))
    elif opt.smaller and ent.size >= opt.smaller:
        add_error('skipping large file "%s"' % ent.name)
    else:
        if stat.S_ISREG(ent.mode):
            try:
                f = open(ent.name)
            except IOError, e:
                add_error(e)
            except OSError, e:
                add_error(e)
            else:
                (mode, id) = hashsplit.split_to_blob_or_tree(w, [f])
        else:
            if stat.S_ISDIR(ent.mode):
                assert(0)  # handled above
            elif stat.S_ISLNK(ent.mode):
                try:
                    rl = os.readlink(ent.name)
                except OSError, e:
                    add_error(e)
                except IOError, e:
                    add_error(e)
                else:
                    (mode, id) = ('120000', w.new_blob(rl))
            else:
                add_error(Exception('skipping special file "%s"' % ent.name))
        if id:
            ent.validate(int(mode, 8), id)
            ent.repack()
            shalists[-1].append((mode, file, id))
    if exists and ent.sha_missing():
        count += oldsize
        subcount = 0


if opt.progress:
    pct = total and count*100.0/total or 100
    progress('Saving: %.2f%% (%d/%dk, %d/%d files), done.    \n'
             % (pct, count/1024, total/1024, fcount, ftotal))

while len(parts) > 1:
    _pop(force_tree = None)
assert(len(shalists) == 1)
tree = w.new_tree(shalists[-1])
if opt.tree:
    print tree.encode('hex')
if opt.commit or opt.name:
    msg = 'bup save\n\nGenerated by command:\n%r' % sys.argv
    ref = opt.name and ('refs/heads/%s' % opt.name) or None
    commit = w.new_commit(oldref, tree, msg)
    if opt.commit:
        print commit.encode('hex')

w.close()  # must close before we can update the ref
        
if opt.name:
    if cli:
        cli.update_ref(refname, commit, oldref)
    else:
        git.update_ref(refname, commit, oldref)

if cli:
    cli.close()

if saved_errors:
    log('WARNING: %d errors encountered while saving.\n' % len(saved_errors))
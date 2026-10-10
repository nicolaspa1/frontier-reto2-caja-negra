import itertools, zlib
samples = {
 "PING":64, "ALTA;Lucia":82, "ALTA;Mateo":3, "CONSULTA;AC-0001":14,
 "DEPOSITO;AC-0001;250000":86, "TRANSFER;AC-0001;AC-0002;40000":86,
 "CONSULTA;AC-0002":11, "TRANSFER;AC-0001;AC-0002;120000":55,
 "TRANSFER;AC-0001;AC-0002;100000":12, "ANULA;OP-0003":66,
 "CONSULTA;AC-0009":87, "TRANSFER;AC-0002;AC-0001;50000":23, "DEPOSITO;AC-0002;700":35,
}
maybe = {"CONSULTa;AC-0001":14, "SALDOS;AC-0001":38}
def variants(s):
    yield "raw", s
    yield "raw;", s+";"
    yield "upper", s.upper()
    yield "upper;", s.upper()+";"
    yield "nosemi", s.replace(";","")
    yield "lower", s.lower()
funcs = {}
funcs["sum"] = lambda b: sum(b)
funcs["xor"] = lambda b: __import__("functools").reduce(lambda a,c:a^c,b,0)
funcs["sum_idx1"] = lambda b: sum((i+1)*c for i,c in enumerate(b))
funcs["sum_idx0"] = lambda b: sum(i*c for i,c in enumerate(b))
funcs["sum_sq"] = lambda b: sum(c*c for c in b)
funcs["crc32"] = lambda b: zlib.crc32(b)
funcs["adler"] = lambda b: zlib.adler32(b)
funcs["sum_digits"] = lambda b: sum(int(ch) for ch in bytes(b).decode() if ch.isdigit())
funcs["sum_alnum"] = lambda b: sum(c for c in b if chr(c).isalnum())
funcs["len"] = lambda b: len(b)
funcs["sum_x_len"] = lambda b: sum(b)*len(b)
funcs["sum_plus_len"] = lambda b: sum(b)+len(b)
funcs["poly31"] = lambda b: __import__("functools").reduce(lambda a,c:(a*31+c)&0xffffffff,b,0)
funcs["poly31_7"] = lambda b: __import__("functools").reduce(lambda a,c:(a*31+c)&0xffffffff,b,7)
funcs["djb2"] = lambda b: __import__("functools").reduce(lambda a,c:((a<<5)+a+c)&0xffffffff,b,5381)
funcs["fnv1a"] = lambda b: __import__("functools").reduce(lambda a,c:((a^c)*16777619)&0xffffffff,b,2166136261)
funcs["sum_weighted_alt"] = lambda b: sum(c*(1 if i%2==0 else 3) for i,c in enumerate(b))
funcs["sum_weighted_alt2"] = lambda b: sum(c*(3 if i%2==0 else 1) for i,c in enumerate(b))
funcs["sum_wrev"] = lambda b: sum((len(b)-i)*c for i,c in enumerate(b))
funcs["sum_ord_minus32"] = lambda b: sum(c-32 for c in b)
funcs["sum_a1"] = lambda b: sum((c-64) if 65<=c<=90 else ((c-96) if 97<=c<=122 else (c-48 if 48<=c<=57 else 0)) for c in b)
hits = []
for vname in ["raw","raw;","upper","upper;","nosemi","lower"]:
    for fname, f in funcs.items():
        for m in list(range(2,257))+[997,1000,10007]:
            for off in (0,1):
                for post in ("mod","modcomp"):
                    ok = True
                    for s, ctl in samples.items():
                        v = dict(variants(s))[vname].encode()
                        r = f(v) % m + off
                        if post=="modcomp": r = (m - (f(v) % m)) % m + off
                        if r != ctl: ok=False; break
                    if ok: hits.append((vname,fname,m,off,post))
print("HITS:", hits)
for h in hits:
    vname,fname,m,off,post = h
    for s,ctl in maybe.items():
        v = dict(variants(s))[vname].encode()
        r = funcs[fname](v)%m+off
        if post=="modcomp": r=(m-(funcs[fname](v)%m))%m+off
        print(h, s, "->", r, "expected", ctl)

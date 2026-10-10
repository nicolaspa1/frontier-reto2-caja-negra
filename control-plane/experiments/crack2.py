samples = {
 "PING":64, "ALTA;Lucia":82, "ALTA;Mateo":3, "CONSULTA;AC-0001":14,
 "DEPOSITO;AC-0001;250000":86, "TRANSFER;AC-0001;AC-0002;40000":86,
 "CONSULTA;AC-0002":11, "TRANSFER;AC-0001;AC-0002;120000":55,
 "TRANSFER;AC-0001;AC-0002;100000":12, "ANULA;OP-0003":66,
 "CONSULTA;AC-0009":87, "TRANSFER;AC-0002;AC-0001;50000":23, "DEPOSITO;AC-0002;700":35,
}
maybe = {"CONSULTa;AC-0001":14, "SALDOS;AC-0001":38, "ALTA;Lucia;Gomez":11}
def norm(s, mode):
    if mode=="upper": s=s.upper()
    if mode=="lower": s=s.lower()
    return s
def iso_digits(s, letters, others):
    out=""
    for ch in s:
        if ch.isdigit(): out+=ch
        elif ch.isalpha():
            if letters=="a10": out+=str(ord(ch.upper())-55)
            elif letters=="a1": out+=str(ord(ch.upper())-64)
            elif letters=="ascii": out+=str(ord(ch))
            elif letters=="drop": pass
        else:
            if others=="ascii": out+=str(ord(ch))
            elif others=="drop": pass
            elif others=="zero": out+="0"
    return out
hits=[]
for mode in ("raw","upper","lower"):
  for letters in ("a10","a1","ascii","drop"):
    for others in ("ascii","drop","zero"):
      for suffix in ("", "00", "0"):
        for m in (97, 11, 10, 7, 13, 89, 101, 100, 99, 37):
          for form in ("mod","98-","97-","m-","m+1-"):
            ok=True
            for s,ctl in samples.items():
                d = iso_digits(norm(s,mode), letters, others)+suffix
                if not d: ok=False;break
                n=int(d)%m
                r = {"mod":n,"98-":98-n,"97-":97-n,"m-":(m-n)%m,"m+1-":m+1-n}[form]
                if r!=ctl: ok=False;break
            if ok: hits.append((mode,letters,others,suffix,m,form))
print("ISO-style HITS:", hits)
# polynomial hashes mod 97 and others on bytes
import itertools
for mode in ("raw","upper","lower"):
  for semi in ("", ";"):
    for m in (97,):
      for B in range(2,200):
        for rev in (False,True):
          for init in (0,1,7):
            for form in ("mod","98-","97-","m-"):
              ok=True
              for s,ctl in samples.items():
                b=(norm(s,mode)+semi).encode()
                if rev: b=b[::-1]
                h=init
                for c in b: h=(h*B+c)%m
                r={"mod":h,"98-":98-h,"97-":97-h,"m-":(m-h)%m}[form]
                if r!=ctl: ok=False;break
              if ok: hits.append(("poly",mode,semi,m,B,rev,init,form))
print("ALL HITS:", hits)
for h in hits:
    if h[0]!="poly":
        mode,letters,others,suffix,m,form=h
        for s,ctl in maybe.items():
            d=iso_digits(norm(s,mode),letters,others)+suffix; n=int(d)%m
            r={"mod":n,"98-":98-n,"97-":97-n,"m-":(m-n)%m,"m+1-":m+1-n}[form]
            print(h,s,"->",r,"expected",ctl)

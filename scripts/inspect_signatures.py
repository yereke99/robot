"""Parse NURSign CMS signatures captured in HAR and print their full
ASN.1 structure. This is the blueprint we'll have to reproduce in Phase 2
(local GOST signing).

Two flavours of signatures appear in goszakup traffic:

  binary mode    -- detached CMS over a file's bytes, NO TSA timestamp
                    used for document signing (Заявка, Категории, ТЗ, Декларация)
  multitext mode -- detached CMS over an XML string, WITH TSA timestamp
                    used for price offers

We pull samples of both from the HAR (response field of WS messages and
request body of POSTs) and print:
  * digest algorithm OID + name
  * signature algorithm OID + name
  * signer cert subject + key OID
  * signed attributes (content type, message digest, signing time...)
  * unsigned attributes (TSA timestamp if any)
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from asn1crypto import cms, x509, tsp, algos
from asn1crypto.core import ObjectIdentifier

HAR = Path(__file__).resolve().parent.parent / "v3bl.goszakup.gov.kz.har"


def collect_signatures() -> dict[str, str]:
    """Return {label: base64_cms} from the HAR.

    Two sources:
      - WS receive messages from NCALayer (binary mode results)
      - POST bodies of ajax_add_priceoffers (multitext mode + TSA)
    """
    har = json.loads(HAR.read_text())
    sigs: dict[str, str] = {}

    for entry in har["log"]["entries"]:
        # WS messages: receive `{"result":"<base64>"}` (binary)
        for msg in entry.get("_webSocketMessages") or []:
            if msg.get("type") != "receive":
                continue
            data = msg.get("data", "")
            try:
                j = json.loads(data)
            except Exception:
                continue
            result = j.get("result")
            if isinstance(result, str) and result.startswith("MII"):
                sigs.setdefault(f"binary_{len(sigs)}", result)
            # multitext: {"result":{"items":{"<id>":"<base64>"}}}
            if isinstance(result, dict) and "items" in result:
                for k, v in result["items"].items():
                    if isinstance(v, str) and v.startswith("MII"):
                        sigs.setdefault(f"multitext_{k}", v)

        # POST request bodies — find signature[N]=... or signData[N]=...
        if entry["request"]["method"] == "POST":
            body = entry["request"].get("postData", {}).get("text", "")
            for m in re.finditer(r"sign(?:ature|Data)%5B(\d+)%5D=([^&]+)", body):
                fid, val_enc = m.group(1), m.group(2)
                from urllib.parse import unquote
                val = unquote(val_enc)
                if val.startswith("MII"):
                    sigs.setdefault(f"post_{fid}", val)

    return sigs


def describe_signature(label: str, b64: str) -> None:
    print(f"\n{'='*78}\n{label}  ({len(b64)} b64 chars)\n{'='*78}")
    raw = base64.b64decode(b64)
    ci = cms.ContentInfo.load(raw)
    print(f"ContentInfo type: {ci['content_type'].native}")

    sd = ci["content"]
    if not isinstance(sd, cms.SignedData):
        print("not SignedData; skipping")
        return

    print(f"version: {sd['version'].native}")
    print(f"digest algorithms: {[a['algorithm'].native for a in sd['digest_algorithms']]}")

    eci = sd["encap_content_info"]
    print(f"encap content type: {eci['content_type'].native}")
    inner = eci["content"]
    if inner is not None and inner.native is not None:
        print(f"encap content present (length {len(inner.native)} bytes)")
    else:
        print("encap content absent (DETACHED signature)")

    # certificates -- tolerant of unknown KZ GOST OIDs
    certs = sd["certificates"]
    print(f"certificates: {len(certs)}")
    for i, c in enumerate(certs):
        choice = c.name
        if choice != "certificate":
            print(f"  [{i}] {choice} (non-x509)")
            continue
        cert = c.chosen
        try:
            print(f"  [{i}] subject: {cert.subject.human_friendly}")
            print(f"      issuer:  {cert.issuer.human_friendly}")
        except Exception as e:
            print(f"  [{i}] (parse error: {e})")
        try:
            # Avoid asn1crypto's spec-lookup that crashes on KZ GOST OIDs:
            # read the raw SubjectPublicKeyInfo without parsing the inner key.
            spki_der = cert["tbs_certificate"]["subject_public_key_info"].dump()
            from asn1crypto.core import Sequence
            spki = Sequence.load(spki_der)
            alg_seq = spki.contents  # skip outer SEQ; field 0 is algorithm
            # Just grab the dotted OID via algorithms module — re-parse the algorithm seq
            from asn1crypto.algos import DigestAlgorithm
            alg_raw = cert["tbs_certificate"]["subject_public_key_info"]["algorithm"].dump()
            alg = DigestAlgorithm.load(alg_raw)
            oid = alg["algorithm"].dotted
            print(f"      pubkey alg OID: {oid}")
        except Exception as e:
            print(f"      pubkey alg: (parse error: {e})")
        try:
            sig_alg = cert["signature_algorithm"]
            print(f"      cert sig alg: {sig_alg['algorithm'].dotted}")
        except Exception as e:
            print(f"      cert sig alg: (parse error: {e})")

    # signer infos
    sis = sd["signer_infos"]
    print(f"signer infos: {len(sis)}")
    for i, si in enumerate(sis):
        print(f"  signer #{i}:")
        print(f"    version: {si['version'].native}")
        print(f"    digest_algorithm: {si['digest_algorithm']['algorithm'].dotted} "
              f"({si['digest_algorithm']['algorithm'].native})")
        print(f"    signature_algorithm: {si['signature_algorithm']['algorithm'].dotted} "
              f"({si['signature_algorithm']['algorithm'].native})")

        sa = si["signed_attrs"]
        sa_list = list(sa) if sa is not None else []
        if sa_list:
            print(f"    signed_attrs: {len(sa_list)}")
            for attr in sa_list:
                t = attr["type"].dotted
                tname = attr["type"].native
                print(f"      • {t}  ({tname})")

        ua = si["unsigned_attrs"]
        ua_list = list(ua) if ua is not None else []
        if ua_list:
            print(f"    unsigned_attrs: {len(ua_list)}")
            for attr in ua_list:
                t = attr["type"].dotted
                tname = attr["type"].native
                print(f"      • {t}  ({tname})")
                # TSA timestamp lives in id-aa-timeStampToken
                if t == "1.2.840.113549.1.9.16.2.14":
                    print(f"        ↳ this is RFC 3161 timestamp token (CAdES-T)")
                    tst_raw = attr["values"][0].dump()
                    try:
                        tst_ci = cms.ContentInfo.load(tst_raw)
                        tst_sd = tst_ci["content"]
                        tst_eci = tst_sd["encap_content_info"]
                        tst_inner = tst_eci["content"].parsed
                        if isinstance(tst_inner, tsp.TSTInfo):
                            print(f"        ↳ TSA hash alg: {tst_inner['message_imprint']['hash_algorithm']['algorithm'].native}")
                            print(f"        ↳ TSA gen time: {tst_inner['gen_time'].native}")
                            print(f"        ↳ TSA policy:   {tst_inner['policy'].native}")
                    except Exception as e:
                        print(f"        ↳ (could not parse TST: {e})")

        sig_value = si["signature"].native
        print(f"    signature bytes: {len(sig_value)} bytes")


def main() -> int:
    sigs = collect_signatures()
    print(f"Found {len(sigs)} signatures in HAR")
    for label in sorted(sigs):
        print(f"  - {label}")

    # describe one of each kind
    binary_sig = next((sigs[k] for k in sigs if k.startswith("binary_")), None)
    if binary_sig:
        describe_signature("BINARY MODE SAMPLE (document signature, no TSA)", binary_sig)

    multitext_sig = next((sigs[k] for k in sigs if k.startswith("multitext_")), None)
    if multitext_sig:
        describe_signature("MULTITEXT MODE SAMPLE (priceoffer, with TSA)", multitext_sig)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

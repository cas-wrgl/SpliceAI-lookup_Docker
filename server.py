from collections import defaultdict
from datetime import datetime
import json
import markdown2
import os
import pandas as pd
import pysam
import re
import redis
import socket
import subprocess
import sys
import tempfile
import time

from flask import Flask, request, Response
from flask_cors import CORS
from flask_talisman import Talisman
from intervaltree import IntervalTree, Interval
from spliceai.utils import Annotator, get_delta_scores

app = Flask(__name__)
Talisman(app)
CORS(app)

HG19_FASTA_PATH = os.path.expanduser("~/hg19.fa")
HG38_FASTA_PATH = os.path.expanduser("~/hg38.fa")

SPLICEAI_CACHE_FILES = {}
if socket.gethostname() == "spliceai-lookup":
    for filename in [
        "spliceai_scores.masked.indel.hg19.vcf.gz",
        "spliceai_scores.masked.indel.hg38.vcf.gz",
        "spliceai_scores.masked.snv.hg19.vcf.gz",
        "spliceai_scores.masked.snv.hg38.vcf.gz",
        "spliceai_scores.raw.indel.hg19.vcf.gz",
        "spliceai_scores.raw.indel.hg38.vcf.gz",
        "spliceai_scores.raw.snv.hg19.vcf.gz",
        "spliceai_scores.raw.snv.hg38.vcf.gz",
    ]:
        key = tuple(filename.replace("spliceai_scores.", "").replace(".vcf.gz", "").split("."))
        full_path = os.path.join("/mnt/disks/cache", filename)
        if os.path.isfile(full_path):
            SPLICEAI_CACHE_FILES[key] = pysam.TabixFile(full_path)
else:
    SPLICEAI_CACHE_FILES = {
        ("raw", "indel", "hg38"): pysam.TabixFile("./test_data/spliceai_scores.raw.indel.hg38_subset.vcf.gz"),
        ("raw", "snv", "hg38"): pysam.TabixFile("./test_data/spliceai_scores.raw.snv.hg38_subset.vcf.gz"),
        ("masked", "snv", "hg38"): pysam.TabixFile("./test_data/spliceai_scores.masked.snv.hg38_subset.vcf.gz"),
    }

GRCH37_ANNOTATIONS = "./annotations/gencode.v38lift37.annotation.txt.gz"
GRCH38_ANNOTATIONS = "./annotations/gencode.v38.annotation.txt.gz"

ANNOTATION_INTERVAL_TREES = {
    "37": defaultdict(IntervalTree),
    "38": defaultdict(IntervalTree),
}

for genome_version, annotation_path in ("37", GRCH37_ANNOTATIONS), ("38", GRCH38_ANNOTATIONS):
    print(f"Loading {annotation_path}", flush=True)
    df = pd.read_table(annotation_path, dtype={"TX_START": int, "TX_END": int})
    for _, row in df.iterrows():
        chrom = row["CHROM"].replace("chr", "")
        ANNOTATION_INTERVAL_TREES[genome_version][chrom].add(Interval(row["TX_START"], row["TX_END"] + 0.1, row["#NAME"]))

SPLICEAI_ANNOTATOR = {
    "37": Annotator(HG19_FASTA_PATH, GRCH37_ANNOTATIONS),
    "38": Annotator(HG38_FASTA_PATH, GRCH38_ANNOTATIONS),
}

SPLICEAI_MAX_DISTANCE_LIMIT = 10000
SPLICEAI_DEFAULT_DISTANCE = 50  # maximum distance between the variant and gained/lost splice site, defaults to 50
SPLICEAI_DEFAULT_MASK = 0  # mask scores representing annotated acceptor/donor gain and unannotated acceptor/donor loss, defaults to 0
USE_PRECOMPUTED_SCORES = 1  # whether to use precomputed scores by default

RATE_LIMIT_WINDOW_SIZE_IN_MINUTES = 1
RATE_LIMIT_REQUESTS_PER_USER_PER_MINUTE = {
    "SpliceAI: computed": 4,
    "SpliceAI: total": 20,
    "liftover: total": 12,
}

DISABLE_LOGGING_FOR_IPS = {f"63.143.42.{i}" for i in range(0, 256)}  # ignore uptimerobot.com IPs

SPLICEAI_SCORE_FIELDS = "ALLELE|SYMBOL|DS_AG|DS_AL|DS_DG|DS_DL|DP_AG|DP_AL|DP_DG|DP_DL".split("|")

SPLICEAI_EXAMPLE = f"/spliceai/?hg=38&distance=50&mask=0&precomputed=1&variant=chr8-140300615-C-G"

VARIANT_RE = re.compile(
    "(chr)?(?P<chrom>[0-9XYMTt]{1,2})"
    "[-\s:]+"
    "(?P<pos>[0-9]{1,9})"
    "[-\s:]+"
    "(?P<ref>[ACGT]+)"
    "[-\s:>]+"
    "(?P<alt>[ACGT]+)"
)

REDIS = redis.Redis(host='localhost', port=6379, db=0)  # in-memory cache server which may or may not be running


def error_response(error_message):
    return Response(json.dumps({"error": str(error_message)}), status=400, mimetype='application/json')


REVERSE_COMPLEMENT_MAP = dict(zip("ACGTN", "TGCAN"))


def reverse_complement(seq):
    return "".join([REVERSE_COMPLEMENT_MAP[n] for n in seq[::-1]])


def parse_variant(variant_str):
    match = VARIANT_RE.match(variant_str)
    if not match:
        raise ValueError(f"Unable to parse variant: {variant_str}")

    return match['chrom'], int(match['pos']), match['ref'], match['alt']


class VariantRecord:
    def __init__(self, chrom, pos, ref, alt):
        self.chrom = chrom
        self.pos = pos
        self.ref = ref
        self.alts = [alt]

    def __repr__(self):
        return f"{self.chrom}-{self.pos}-{self.ref}-{self.alts[0]}"


def get_spliceai_redis_key(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores):
    return f"{variant}__hg{genome_version}__d{spliceai_distance}__m{spliceai_mask}__pre{use_precomputed_scores}"


def get_spliceai_from_redis(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores):
    key = get_spliceai_redis_key(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores)
    results = None
    try:
        results_string = REDIS.get(key)
        if results_string:
            results = json.loads(results_string)
            results["source"] += ":redis"
    except Exception as e:
        print(f"Redis error: {e}", flush=True)

    return results


def add_spliceai_to_redis(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores, results):
    key = get_spliceai_redis_key(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores)
    try:
        results_string = json.dumps(results)
        REDIS.set(key, results_string)
    except Exception as e:
        print(f"Redis error: {e}", flush=True)


def exceeds_rate_limit(user_id, request_type):
    """Checks whether the given address has exceeded rate limits

    Args:
        user_id (str): unique user id
        request_type (str): type of rate limit - can be "SpliceAI: total", "SpliceAI: computed", or "liftover: total"

    Return (bool): True if the given user has exceeded the rate limit for this request type.
    """
    if request_type not in RATE_LIMIT_REQUESTS_PER_USER_PER_MINUTE:
        raise ValueError(f"Invalid 'request_type' arg value: {request_type}")

    max_requests_per_minute = RATE_LIMIT_REQUESTS_PER_USER_PER_MINUTE[request_type]
    max_requests = RATE_LIMIT_WINDOW_SIZE_IN_MINUTES * max_requests_per_minute

    epoch_time = time.time()  # seconds since 1970
    try:
        # check number of requests from this user in the last (RATE_LIMIT_WINDOW_SIZE_IN_MINUTES * 60) minutes
        redis_key_prefix = f"request {user_id} {request_type}"
        keys = REDIS.keys(f"{redis_key_prefix}*")
        if len(keys) >= max_requests:
            return True

        # record this request
        REDIS.set(f"{redis_key_prefix}: {epoch_time}", 1)
        REDIS.expire(f"{redis_key_prefix}: {epoch_time}", RATE_LIMIT_WINDOW_SIZE_IN_MINUTES * 60)
    except Exception as e:
        print(f"Redis error: {e}", flush=True)

    return False


def process_variant(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores):
    try:
        chrom, pos, ref, alt = parse_variant(variant)
    except ValueError as e:
        return {
            "variant": variant,
            "error": f"ERROR: {e}",
        }

    if len(ref) > 1 and len(alt) > 1:
        return {
            "variant": variant,
            "error": f"ERROR: SpliceAI does not currently support complex InDels like {chrom}-{pos}-{ref}-{alt}",
        }

    # generate error message if variant falls outside annotated exons or introns
    OTHER_GENOME_VERSION = {"37": "38", "38": "37"}
    chrom_without_chr = chrom.replace("chr", "")
    if not ANNOTATION_INTERVAL_TREES[genome_version][chrom_without_chr].at(pos):
        other_genome_version = OTHER_GENOME_VERSION[genome_version]
        other_genome_overlapping_intervals = ANNOTATION_INTERVAL_TREES[other_genome_version][chrom_without_chr].at(pos)
        if other_genome_overlapping_intervals:
            other_genome_genes = " and ".join(sorted(set([str(i.data).split("---")[0] for i in other_genome_overlapping_intervals])))
            return {
                "variant": variant,
                "error": f"ERROR: In GRCh{genome_version}, {chrom}-{pos}-{ref}-{alt} falls outside all gencode exons and introns."
                         f"SpliceAI only works for variants within known exons or introns. However, in GRCh{other_genome_version}, "
                         f"{chrom}:{pos} falls within {other_genome_genes}, so perhaps GRCh{genome_version} is not the correct genome version?"
            }
        else:
            return {
                "variant": variant,
                "error": f"ERROR: {chrom}-{pos}-{ref}-{alt} falls outside all Gencode exons and introns on "
                f"GRCh{genome_version}. SpliceAI only works for variants that are within known exons or introns.",
            }

            """
            NOTE: The reason SpliceAI currently works only for variants "
                         f"within annotated exons or introns is that, although the SpliceAI neural net takes any "
                         f"arbitrary nucleotide sequence as input, SpliceAI needs 1) the transcript strand "
                         f"to determine whether to reverse-complement the reference genome sequence before passing it "
                         f"to the neural net, and 2) transcript start and end positions to determine where to truncate "
                         f"the reference genome sequence.
            """

    source = None
    scores = []
    if (len(ref) <= 5 or len(alt) <= 2) and str(spliceai_distance) == str(SPLICEAI_DEFAULT_DISTANCE) and str(use_precomputed_scores) == "1":
        # examples: ("masked", "snv", "hg19")  ("raw", "indel", "hg38")
        key = (
            "masked" if str(spliceai_mask) == "1" else ("raw" if str(spliceai_mask) == "0" else None),
            "snv" if len(ref) == 1 and len(alt) == 1 else "indel",
            "hg19" if genome_version == "37" else ("hg38" if genome_version == "38" else None),
        )
        try:
            results = SPLICEAI_CACHE_FILES[key].fetch(chrom, pos-1, pos+1)
            for line in results:
                # ['1', '739023', '.', 'C', 'CT', '.', '.', 'SpliceAI=CT|AL669831.1|0.00|0.00|0.00|0.00|-1|-37|-48|-37']
                fields = line.split("\t")
                if fields[0] == chrom and int(fields[1]) == pos and fields[3] == ref and fields[4] == alt:
                    scores.append(fields[7])
            if scores:
                source = "lookup"
                #print(f"Fetched: ", scores, flush=True)

        except Exception as e:
            print(f"ERROR: couldn't retrieve scores using tabix: {type(e)}: {e}", flush=True)

    if not scores:
        if exceeds_rate_limit(request.remote_addr, request_type="SpliceAI: computed"):
            return {
                "variant": variant,
                "error": f"ERROR: Rate limit reached. To prevent a user from overwhelming the server and making it "
                         f"unavailable to other users, this tool allows no more than "
                         f"{RATE_LIMIT_REQUESTS_PER_USER_PER_MINUTE['SpliceAI: computed']} computed requests per minute per user.",
            }

        record = VariantRecord(chrom, pos, ref, alt)
        try:
            scores = get_delta_scores(
                record,
                SPLICEAI_ANNOTATOR[genome_version],
                spliceai_distance,
                spliceai_mask)
            source = "computed"
            #print(f"Computed: ", scores, flush=True)
        except Exception as e:
            return {
                "variant": variant,
                "error": f"ERROR: {type(e)}: {e}",
            }

    if not scores:
        return {
            "variant": variant,
            "error": f"ERROR: The SpliceAI model did not return any scores for {variant}. This is typically due to the "
                     f"variant not being within any exon or intron as defined in Gencode v36",
        }

    scores = [s[s.index("|")+1:] for s in scores]  # drop allele field

    return {
        "variant": variant,
        "genome_version": genome_version,
        "chrom": chrom,
        "pos": pos,
        "ref": ref,
        "alt": alt,
        "scores": scores,
        "source": source,
    }


@app.route("/spliceai/", methods=['POST', 'GET'])
def run_spliceai():
    start_time = datetime.now()
    logging_prefix = start_time.strftime("%m/%d/%Y %H:%M:%S") + f" t{os.getpid()}"

    # check params
    params = {}
    if request.values:
        params.update(request.values)

    if 'variant' not in params:
        params.update(request.get_json(force=True, silent=True) or {})

    if exceeds_rate_limit(request.remote_addr, request_type="SpliceAI: total"):
        error_message = (f"ERROR: Rate limit reached. To prevent a user from overwhelming the server and making it "
            f"unavailable to other users, this tool allows no more than "
            f"{RATE_LIMIT_REQUESTS_PER_USER_PER_MINUTE['SpliceAI: total']} requests per minute per user.")

        print(f"{logging_prefix}: {request.remote_addr}: response: {error_message}", flush=True)
        return error_response(error_message)

    variant = params.get('variant', '')
    variant = variant.strip().strip("'").strip('"').strip(",")
    if not variant:
        return error_response(f'"variant" not specified. For example: {SPLICEAI_EXAMPLE}\n')

    if not isinstance(variant, str):
        return error_response(f'"variant" value must be a string rather than a {type(variant)}.\n')

    genome_version = params.get("hg")
    if not genome_version:
        return error_response(f'"hg" not specified. The URL must include an "hg" arg: hg=37 or hg=38. For example: {SPLICEAI_EXAMPLE}\n')

    if genome_version not in ("37", "38"):
        return error_response(f'Invalid "hg" value: "{genome_version}". The value must be either "37" or "38". For example: {SPLICEAI_EXAMPLE}\n')

    spliceai_distance = params.get("distance", SPLICEAI_DEFAULT_DISTANCE)
    try:
        spliceai_distance = int(spliceai_distance)
    except Exception as e:
        return error_response(f'Invalid "distance": "{spliceai_distance}". The value must be an integer.\n')

    if spliceai_distance > SPLICEAI_MAX_DISTANCE_LIMIT:
        return error_response(f'Invalid "distance": "{spliceai_distance}". The value must be < {SPLICEAI_MAX_DISTANCE_LIMIT}.\n')

    spliceai_mask = params.get("mask", str(SPLICEAI_DEFAULT_MASK))
    if spliceai_mask not in ("0", "1"):
        return error_response(f'Invalid "mask" value: "{spliceai_mask}". The value must be either "0" or "1". For example: {SPLICEAI_EXAMPLE}\n')

    spliceai_mask = int(spliceai_mask)

    use_precomputed_scores = params.get("precomputed", str(USE_PRECOMPUTED_SCORES))
    if use_precomputed_scores not in ("0", "1"):
        return error_response(f'Invalid "precomputed" value: "{use_precomputed_scores}". The value must be either "0" or "1". For example: {SPLICEAI_EXAMPLE}\n')

    use_precomputed_scores = int(use_precomputed_scores)

    if request.remote_addr not in DISABLE_LOGGING_FOR_IPS:
        print(f"{logging_prefix}: {request.remote_addr}: ======================", flush=True)
        print(f"{logging_prefix}: {request.remote_addr}: {variant} processing with hg={genome_version}, "
              f"distance={spliceai_distance}, mask={spliceai_mask}, precomputed={use_precomputed_scores}", flush=True)

    # check REDIS cache before processing the variant
    results = get_spliceai_from_redis(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores)
    if not results:
        results = process_variant(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores)
        if "error" not in results:
            add_spliceai_to_redis(variant, genome_version, spliceai_distance, spliceai_mask, use_precomputed_scores, results)

    status = 400 if results.get("error") else 200

    response_json = {}
    response_json.update(params)  # copy input params to output
    response_json.update(results)

    duration = str(datetime.now() - start_time)
    response_json['duration'] = duration

    if request.remote_addr not in DISABLE_LOGGING_FOR_IPS:
        print(f"{logging_prefix}: {request.remote_addr}: {variant} response: {response_json}", flush=True)
        print(f"{logging_prefix}: {request.remote_addr}: {variant} took {duration}", flush=True)

    return Response(json.dumps(response_json), status=status, mimetype='application/json')


LIFTOVER_EXAMPLE = f"/liftover/?hg=hg19-to-hg38&format=interval&chrom=chr8&start=140300615&end=140300620"

CHAIN_FILE_PATHS = {
    "hg19-to-hg38": "hg19ToHg38.over.chain.gz",
    "hg38-to-hg19": "hg38ToHg19.over.chain.gz",
    "hg38-to-t2t": "hg38.t2t-chm13-v1.0.over.chain.gz",
    "t2t-to-hg38": "t2t-chm13-v1.0.hg38.over.chain.gz",
}


def run_UCSC_liftover_tool(hg, chrom, start, end, verbose=False):
    if hg not in CHAIN_FILE_PATHS:
        raise ValueError(f"Unexpected hg arg value: {hg}")
    chain_file_path = CHAIN_FILE_PATHS[hg]

    reason_liftover_failed = ""
    with tempfile.NamedTemporaryFile(suffix=".bed", mode="wt", encoding="UTF-8") as input_file, \
        tempfile.NamedTemporaryFile(suffix=".bed", mode="rt", encoding="UTF-8") as output_file, \
        tempfile.NamedTemporaryFile(suffix=".bed", mode="rt", encoding="UTF-8") as unmapped_output_file:

        #  command syntax: liftOver oldFile map.chain newFile unMapped
        chrom = "chr" + chrom.replace("chr", "")
        input_file.write("\t".join(map(str, [chrom, start, end, ".", "0", "+"])) + "\n")
        input_file.flush()
        command = f"liftOver {input_file.name} {chain_file_path} {output_file.name} {unmapped_output_file.name}"

        try:
            subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, encoding="UTF-8")
            results = output_file.read()

            if verbose:
                print(f"{hg} liftover on {chrom}:{start}-{end} returned: {results}", flush=True)

            result_fields = results.strip().split("\t")
            if len(result_fields) > 5:
                result_fields[1] = int(result_fields[1])
                result_fields[2] = int(result_fields[2])

                return {
                    "chrom": chrom,
                    "start": start,
                    "end": end,
                    "output_chrom": result_fields[0],
                    "output_start": result_fields[1],
                    "output_end": result_fields[2],
                    "output_strand": result_fields[5],
                }
            else:
                reason_liftover_failed = unmapped_output_file.readline().replace("#", "").strip()
        except Exception as e:
            raise ValueError(f"{hg} liftOver command failed for {chrom}:{start}-{end}: {e}")

    if reason_liftover_failed:
        raise ValueError(f"{hg} liftover failed for {chrom}:{start}-{end} {reason_liftover_failed}")
    else:
        raise ValueError(f"{hg} liftover failed for {chrom}:{start}-{end} for unknown reasons")


def get_liftover_redis_key(genome_version, chrom, start, end):
    return f"liftover_hg{genome_version}__{chrom}_{start}_{end}"


def get_liftover_from_redis(hg, chrom, start, end):
    key = get_liftover_redis_key(hg, chrom, start, end)
    results = None
    try:
        results_string = REDIS.get(key)
        if results_string:
            results = json.loads(results_string)
    except Exception as e:
        print(f"Redis error: {e}", flush=True)

    return results


def add_liftover_to_redis(hg, chrom, start, end, result):
    key = get_liftover_redis_key(hg, chrom, start, end)
    try:
        results_string = json.dumps(result)
        REDIS.set(key, results_string)
    except Exception as e:
        print(f"Redis error: {e}", flush=True)


@app.route("/liftover/", methods=['POST', 'GET'])
def run_liftover():
    logging_prefix = datetime.now().strftime("%m/%d/%Y %H:%M:%S") + f" t{os.getpid()}"

    # check params
    params = {}
    if request.values:
        params.update(request.values)

    if "format" not in params:
        params.update(request.get_json(force=True, silent=True) or {})

    if exceeds_rate_limit(request.remote_addr, request_type="liftover: total"):
        error_message = (f"ERROR: Rate limit reached. To prevent a user from overwhelming the server and making it "
                         f"unavailable to other users, this tool allows no more than "
                         f"{RATE_LIMIT_REQUESTS_PER_USER_PER_MINUTE['liftover: total']} liftover requests per minute per user.")

        print(f"{logging_prefix}: {request.remote_addr}: response: {error_message}", flush=True)
        return error_response(error_message)

    VALID_HG_VALUES = set(CHAIN_FILE_PATHS.keys())
    hg = params.get("hg")
    if not hg or hg not in VALID_HG_VALUES:
        return error_response(f'"hg" param error. It should be set to {" or ".join(VALID_HG_VALUES)}. For example: {LIFTOVER_EXAMPLE}\n')

    VALID_FORMAT_VALUES = ("interval", "variant", "position")
    format = params.get("format", "")
    if not format or format not in VALID_FORMAT_VALUES:
        return error_response(f'"format" param error. It should be set to {" or ".join(VALID_FORMAT_VALUES)}. For example: {LIFTOVER_EXAMPLE}\n')

    chrom = params.get("chrom")
    if not chrom:
        return error_response(f'"chrom" param not specified')

    if format == "interval":
        start = params.get("start")
        end = params.get("end")
        if not start:
            return error_response(f'"start" param not specified')
        if not end:
            return error_response(f'"end" param not specified')
        variant_log_string = f"{start}-{end}"

    elif format == "position" or format == "variant":
        pos = params.get("pos")
        if not pos:
            return error_response(f'"pos" param not specified')

        pos = int(pos)
        start = pos - 1
        end = pos
        variant_log_string = f"{pos} "
        if params.get('ref') and params.get('alt'):
            variant_log_string += f"{params.get('ref')}>{params.get('alt')}"

    verbose = request.remote_addr not in DISABLE_LOGGING_FOR_IPS
    if verbose:
        print(f"{logging_prefix}: {request.remote_addr}: ======================", flush=True)
        print(f"{logging_prefix}: {request.remote_addr}: {hg} liftover {format}: {chrom}:{variant_log_string}", flush=True)

    # check REDIS cache before processing the variant
    result = get_liftover_from_redis(hg, chrom, start, end)
    if result and verbose:
        print(f"{hg} liftover on {chrom}:{start}-{end} got results from cache: {result}", flush=True)

    if not result:
        try:
            result = run_UCSC_liftover_tool(hg, chrom, start, end, verbose=verbose)
        except Exception as e:
            return error_response(str(e))
    
        add_liftover_to_redis(hg, chrom, start, end, result)

    result.update(params)
    if format == "position" or format == "variant":
        result["pos"] = pos
        result["output_pos"] = result["output_end"]

    if format == "variant":
        result["output_ref"] = result["ref"]
        result["output_alt"] = result["alt"]
        if result["output_strand"] == "-":
            result["output_ref"] = reverse_complement(result["output_ref"])
            result["output_alt"] = reverse_complement(result["output_alt"])

    return Response(json.dumps(result), mimetype='application/json')


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>/')
def catch_all(path):
    with open("README.md") as f:
        return markdown2.markdown(f.read())


print("Initialization completed.", flush=True)

if __name__ == "__main__":
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

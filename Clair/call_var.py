import sys
import os
import time
import argparse
import param
import logging
import numpy as np
from threading import Thread
from math import log, e
from enum import IntEnum
from collections import namedtuple, defaultdict
from itertools import izip

import utils
import clair_model as cv
from utils import GT21, base_change_label_from, Genotype, genotype_string_from, VariantLength

import pysam

logging.basicConfig(format='%(message)s', level=logging.INFO)
num2base = dict(zip((0, 1, 2, 3), "ACGT"))
base2num = dict(zip("ACGT", (0, 1, 2, 3)))
minimum_variant_length_that_need_infer = VariantLength.max
maximum_variant_length_that_need_infer = 50
inferred_indel_length_minimum_allele_frequency = 0.125
flanking_base_number = param.flankingBaseNum


class Channel(IntEnum):
    reference = 0
    insert = 1
    delete = 2
    SNP = 3


def homo_SNP_bases_from(base_change_probabilities):
    output_bases_probabilities = np.array([
        base_change_probabilities[GT21.AA],
        base_change_probabilities[GT21.CC],
        base_change_probabilities[GT21.GG],
        base_change_probabilities[GT21.TT],
    ])
    output_bases = [
        base_change_label_from(GT21.AA),
        base_change_label_from(GT21.CC),
        base_change_label_from(GT21.GG),
        base_change_label_from(GT21.TT)
    ][np.argmax(output_bases_probabilities)]
    return output_bases[0], output_bases[1]


def hetero_SNP_bases_from(base_change_probabilities):
    output_bases_probabilities = np.array([
        base_change_probabilities[GT21.AC],
        base_change_probabilities[GT21.AG],
        base_change_probabilities[GT21.AT],
        base_change_probabilities[GT21.CG],
        base_change_probabilities[GT21.CT],
        base_change_probabilities[GT21.GT]
    ])
    output_bases = [
        base_change_label_from(GT21.AC),
        base_change_label_from(GT21.AG),
        base_change_label_from(GT21.AT),
        base_change_label_from(GT21.CG),
        base_change_label_from(GT21.CT),
        base_change_label_from(GT21.GT)
    ][np.argmax(output_bases_probabilities)]
    return output_bases[0], output_bases[1]


def hetero_insert_base_from(base_change_probabilities):
    output_bases_probabilities = np.array([
        base_change_probabilities[GT21.AIns],
        base_change_probabilities[GT21.CIns],
        base_change_probabilities[GT21.GIns],
        base_change_probabilities[GT21.TIns]
    ])
    output_bases = [
        base_change_label_from(GT21.AIns),
        base_change_label_from(GT21.CIns),
        base_change_label_from(GT21.GIns),
        base_change_label_from(GT21.TIns)
    ][np.argmax(output_bases_probabilities)]
    return output_bases[0]


def hetero_delete_base_from(base_change_probabilities):
    output_bases_probabilities = np.array([
        base_change_probabilities[GT21.ADel],
        base_change_probabilities[GT21.CDel],
        base_change_probabilities[GT21.GDel],
        base_change_probabilities[GT21.TDel]
    ])
    output_bases = [
        base_change_label_from(GT21.ADel),
        base_change_label_from(GT21.CDel),
        base_change_label_from(GT21.GDel),
        base_change_label_from(GT21.TDel)
    ][np.argmax(output_bases_probabilities)]
    return output_bases[0]


def filtration_value_from(quality_score_for_pass, quality_score):
    if quality_score_for_pass is None:
        return "."
    if quality_score >= quality_score_for_pass:
        return "PASS"
    return "LowQual"


def pileup(sam_file, contig, position_start, position_end, func):
    """
    Pileup using pysam

    sam_file: pysam.AlignmentFile for pileup
    contig: chromosome name or contig name
    position_start: start position. 0-based. Inclusive.
    position_end: ending position. 0-based. Exclusive.
    func: callback for pileup_column
    """
    try:
        for pileup_column in sam_file.pileup(
            contig,
            start=position_start,
            stop=position_end,
            flag_filter=2308,
            min_base_quality=0,
            max_depth=250
        ):
            func(pileup_column)
    except AssertionError:
        pass


def insertion_bases_using_pysam_from(
    sam_file,
    contig,
    position,
    minimum_insertion_length=1,
    maximum_insertion_length=maximum_variant_length_that_need_infer,
    insertion_bases_to_ignore=""
):
    insertion_bases_dict = defaultdict(lambda: 0)

    def high_order_func(pileup_column):
        if pileup_column.reference_pos != position - 1:
            return

        for sequence in pileup_column.get_query_sequences(mark_matches=False, mark_ends=False, add_indels=True):
            # minimum sequence needed: A+1A, and "+" for insertion
            if len(sequence) < 4 or sequence[1] != "+":
                continue

            no_of_insertion_bases = 0
            for (string_index, c) in enumerate(sequence[2:]):
                if not c.isdigit():
                    insertion_bases = sequence[string_index+2:].upper()
                    break
                no_of_insertion_bases = no_of_insertion_bases * 10 + int(c)

            if (
                minimum_insertion_length <= no_of_insertion_bases <= maximum_insertion_length and
                insertion_bases != insertion_bases_to_ignore
            ):
                insertion_bases_dict[insertion_bases] = insertion_bases_dict[insertion_bases] + 1
    pileup(sam_file, contig, position, position+1, func=high_order_func)

    return max(insertion_bases_dict, key=insertion_bases_dict.get) if len(insertion_bases_dict) > 0 else ""


def deletion_bases_using_pysam_from(
    sam_file,
    fasta_file,
    contig,
    position,
    minimum_deletion_length=1,
    maximum_deletion_length=maximum_variant_length_that_need_infer
):
    deletion_bases_dict = defaultdict(lambda: 0)

    def high_order_func(pileup_column):
        if pileup_column.reference_pos != position - 1:
            return

        for sequence in pileup_column.get_query_sequences(mark_matches=False, mark_ends=False, add_indels=True):
            # minimum sequence needed: A-1A, and "-" for deletion
            if len(sequence) < 4 or sequence[1] != "-":
                continue

            no_of_deletion_bases = 0
            for c in sequence[2:]:
                if not c.isdigit():
                    deletion_bases = fasta_file.fetch(
                        reference=contig, start=position, end=position + no_of_deletion_bases
                    )
                    break
                no_of_deletion_bases = no_of_deletion_bases * 10 + int(c)

            if minimum_deletion_length <= no_of_deletion_bases <= maximum_deletion_length:
                deletion_bases_dict[deletion_bases] = deletion_bases_dict[deletion_bases] + 1
    pileup(sam_file, contig, position, position+1, func=high_order_func)

    return max(deletion_bases_dict, key=deletion_bases_dict.get) if len(deletion_bases_dict) > 0 else ""


def Run(args):
    utils.setup_environment()

    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"

    if args.threads == None:
        if args.tensor_fn == "PIPE":
            param.NUM_THREADS = 4
    else:
        param.NUM_THREADS = args.threads
        param.NUM_THREADS -= 1
        if param.NUM_THREADS < 1:
            param.NUM_THREADS = 1

    m = cv.Clair()
    m.init()
    m.restore_parameters(os.path.abspath(args.chkpnt_fn))

    if args.activation_only:
        log_activation(args, m, utils)
    else:
        Test(args, m, utils)


def print_debug_message_with(
    is_debug,
    call_fh,
    chromosome,
    position,
    base_change_probabilities,
    genotype_probabilities,
    variant_length_probabilities_1,
    variant_length_probabilities_2,
    extra_infomation_string=""
):
    if not is_debug:
        return

    print >> call_fh, "{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
        chromosome,
        position,
        ["{:0.8f}".format(x) for x in base_change_probabilities],
        ["{:0.8f}".format(x) for x in genotype_probabilities],
        ["{:0.8f}".format(x) for x in variant_length_probabilities_1],
        ["{:0.8f}".format(x) for x in variant_length_probabilities_2],
        extra_infomation_string
    )


def insertion_length_tuple_from(
    variant_length_probabilities_1,
    variant_length_probabilities_2,
    is_hetero_Ins=False,
    is_hetero_InsIns=False
):
    """
    get tuple with values:
        - variant_length 1 and 2 for variant calling
        - probability for selected base length combinations
        - variant_length_1 <= variant_length_2
    """
    maximum_probability = 0

    # ACGT Ins
    if is_hetero_Ins:
        variant_length = 0
        for i in xrange(1, VariantLength.max + 1):
            temp_probability = (
                variant_length_probabilities_1[0 + VariantLength.index_offset] *
                variant_length_probabilities_2[i + VariantLength.index_offset]
            )
            if temp_probability > maximum_probability:
                variant_length = i
                maximum_probability = temp_probability
            temp_probability = (
                variant_length_probabilities_1[i + VariantLength.index_offset] *
                variant_length_probabilities_2[0 + VariantLength.index_offset]
            )
            if temp_probability > maximum_probability:
                variant_length = i
                maximum_probability = temp_probability
        return 0, variant_length, maximum_probability

    # hetero InsIns
    if is_hetero_InsIns:
        variant_length_1, variant_length_2 = 0, 0
        for i in xrange(1, VariantLength.max + 1):
            for j in xrange(1, VariantLength.max + 1):
                # note: one kind of InsIns is same # of insertion bases but different kind of ACGT
                temp_probability = (
                    variant_length_probabilities_1[i + VariantLength.index_offset] *
                    variant_length_probabilities_2[j + VariantLength.index_offset]
                )
                if temp_probability > maximum_probability:
                    variant_length_1, variant_length_2 = (i, j) if i <= j else (j, i)
                    maximum_probability = temp_probability
        return variant_length_1, variant_length_2, maximum_probability

    # homo Ins
    variant_length = 0
    for i in xrange(1, VariantLength.max + 1):
        temp_probability = (
            variant_length_probabilities_1[i + VariantLength.index_offset] *
            variant_length_probabilities_2[i + VariantLength.index_offset]
        )
        if temp_probability <= maximum_probability:
            continue
        variant_length = i
        maximum_probability = temp_probability
    return variant_length, variant_length, maximum_probability


def deletion_length_tuple_from(
    variant_length_probabilities_1,
    variant_length_probabilities_2,
    is_hetero_Del=False,
    is_hetero_DelDel=False,
):
    """
    get tuple with values:
        - variant length 1 and 2 for variant calling
        - probability for selected base length combinations
    """
    maximum_probability = 0

    # ACGT Del
    if is_hetero_Del:
        variant_length = 0
        for i in xrange(1, VariantLength.max + 1):
            temp_probability = (
                variant_length_probabilities_1[0 + VariantLength.index_offset] *
                variant_length_probabilities_2[-i + VariantLength.index_offset]
            )
            if temp_probability > maximum_probability:
                variant_length = i
                maximum_probability = temp_probability
            temp_probability = (
                variant_length_probabilities_1[-i + VariantLength.index_offset] *
                variant_length_probabilities_2[0 + VariantLength.index_offset]
            )
            if temp_probability > maximum_probability:
                variant_length = i
                maximum_probability = temp_probability
        return 0, variant_length, maximum_probability

    # hetero DelDel
    if is_hetero_DelDel:
        variant_length_1, variant_length_2 = 0, 0
        for i in xrange(1, VariantLength.max + 1):
            for j in xrange(1, VariantLength.max + 1):
                if i == j:
                    continue
                temp_probability = (
                    variant_length_probabilities_1[-i + VariantLength.index_offset] *
                    variant_length_probabilities_2[-j + VariantLength.index_offset]
                )
                if temp_probability > maximum_probability:
                    variant_length_1, variant_length_2 = (i, j) if i <= j else (j, i)
                    maximum_probability = temp_probability
        return variant_length_1, variant_length_2, maximum_probability

    # homo Del
    variant_length = 0
    for i in xrange(1, VariantLength.max + 1):
        temp_probability = (
            variant_length_probabilities_1[-i + VariantLength.index_offset] *
            variant_length_probabilities_2[-i + VariantLength.index_offset]
        )
        if temp_probability <= maximum_probability:
            continue
        variant_length = i
        maximum_probability = temp_probability
    return variant_length, variant_length, maximum_probability


def insertion_and_deletion_length_tuple_from(variant_length_probabilities_1, variant_length_probabilities_2):
    """
    get tuple with values:
        - variant length 1 for variant calling (deletion)
        - variant_length 2 for variant calling (insertion)
        - probability for selected base length combinations
    """
    maximum_probability = 0
    variant_length_1, variant_length_2 = -1, -1
    for i in xrange(1, VariantLength.max + 1):
        for j in xrange(1, VariantLength.max + 1):
            temp_probability = (
                variant_length_probabilities_1[i + VariantLength.index_offset] *
                variant_length_probabilities_2[-j + VariantLength.index_offset]
            )
            if temp_probability > maximum_probability:
                maximum_probability = temp_probability
                variant_length_1, variant_length_2 = j, i
            temp_probability = (
                variant_length_probabilities_1[-i + VariantLength.index_offset] *
                variant_length_probabilities_2[j + VariantLength.index_offset]
            )
            if temp_probability > maximum_probability:
                maximum_probability = temp_probability
                variant_length_1, variant_length_2 = i, j
    return variant_length_1, variant_length_2, maximum_probability


def inferred_insertion_bases_from(tensor_input):
    insertion_bases = ""
    for position in xrange(flanking_base_number + 1, 2 * flanking_base_number + 1):
        reference_tensor = tensor_input[position, :, Channel.reference]
        insertion_tensor = np.copy(tensor_input[position, :, Channel.insert])
        for base_index in range(0, 4):
            insertion_tensor[base_index] = insertion_tensor[base_index] + insertion_tensor[base_index + 4]
            insertion_tensor[base_index + 4] = 0
        if (
            position < (flanking_base_number + minimum_variant_length_that_need_infer) or
            sum(insertion_tensor) >= inferred_indel_length_minimum_allele_frequency * sum(reference_tensor)
        ):
            insertion_bases += num2base[np.argmax(insertion_tensor) % 4]
        else:
            break
    return insertion_bases


def inferred_deletion_length_from(tensor_input):
    deletion_length = 0
    for position in xrange(flanking_base_number + 1, 2 * flanking_base_number + 1):
        reference_tensor = tensor_input[position, :, Channel.reference]
        deletion_tensor = tensor_input[position, :, Channel.delete]
        if (
            position < (flanking_base_number + minimum_variant_length_that_need_infer) or
            sum(deletion_tensor) >= inferred_indel_length_minimum_allele_frequency * sum(reference_tensor)
        ):
            deletion_length += 1
        else:
            break
    return deletion_length


def insertion_bases_using_tensor(tensor_input, variant_length):
    insertion_bases = ""
    for position in range(flanking_base_number + 1, flanking_base_number + variant_length + 1):
        insertion_tensor = np.copy(tensor_input[position, :, Channel.insert])
        for base_index in range(0, 4):
            insertion_tensor[base_index] = insertion_tensor[base_index] + insertion_tensor[base_index + 4]
            insertion_tensor[base_index + 4] = 0
        insertion_bases += num2base[np.argmax(insertion_tensor) % 4]
    return insertion_bases


def maximum_variant_length_from(variant_length):
    if variant_length >= minimum_variant_length_that_need_infer:
        return maximum_variant_length_that_need_infer
    else:
        return variant_length


def insertion_bases_from(
    tensor_input,
    variant_length,
    sam_file,
    contig,
    position,
    is_using_pysam_for_all_indel_bases_output
):
    """
        Return (insertion_bases, insertion bases length, is_inferred) tuple
    """
    if is_using_pysam_for_all_indel_bases_output:
        insertion_bases = insertion_bases_using_pysam_from(
            sam_file=sam_file,
            contig=contig,
            position=position,
            minimum_insertion_length=variant_length,
            maximum_insertion_length=maximum_variant_length_from(variant_length)
        )
        return insertion_bases, len(insertion_bases), False

    need_inferred_variant_length = variant_length >= minimum_variant_length_that_need_infer
    if not need_inferred_variant_length:
        insertion_bases = insertion_bases_using_tensor(tensor_input, variant_length)
        return insertion_bases, len(insertion_bases), False

    insertion_bases = insertion_bases_using_pysam_from(
        sam_file=sam_file,
        contig=contig,
        position=position,
        minimum_insertion_length=minimum_variant_length_that_need_infer
    )
    insertion_length = len(insertion_bases)
    if insertion_length > 0:
        return insertion_bases, insertion_length, False
    else:
        insertion_bases = inferred_insertion_bases_from(tensor_input)
        return insertion_bases, len(insertion_bases), True


def deletion_bases_from(
    tensor_input,
    variant_length,
    sam_file,
    fasta_file,
    contig,
    position,
    reference_sequence,
    is_using_pysam_for_all_indel_bases_output
):
    """
        Return (deletion_bases, deletion bases length, is_inferred) tuple
    """
    if is_using_pysam_for_all_indel_bases_output:
        deletion_bases = deletion_bases_using_pysam_from(
            sam_file=sam_file,
            fasta_file=fasta_file,
            contig=contig,
            position=position,
            minimum_deletion_length=variant_length,
            maximum_deletion_length=maximum_variant_length_from(variant_length)
        )
        return deletion_bases, len(deletion_bases), False

    deletion_bases = ""
    need_inferred_variant_length = variant_length >= minimum_variant_length_that_need_infer
    if need_inferred_variant_length:
        deletion_bases = deletion_bases_using_pysam_from(
            sam_file=sam_file,
            fasta_file=fasta_file,
            contig=contig,
            position=position,
            minimum_deletion_length=minimum_variant_length_that_need_infer
        )

    have_long_deletion_bases = need_inferred_variant_length and len(deletion_bases) >= flanking_base_number
    if have_long_deletion_bases:
        return deletion_bases, len(deletion_bases), False
    else:
        deletion_bases = reference_sequence[flanking_base_number + 1:flanking_base_number + variant_length + 1]
        return deletion_bases, len(deletion_bases), need_inferred_variant_length


def quality_score_from(
    reference_base,
    alternate_base,
    genotype_string,
    gt21_probabilities,
    genotype_probabilities,
):
    genotype_1, genotype_2 = int(genotype_string[0]), int(genotype_string[2])
    if genotype_1 > genotype_2:
        genotype_1, genotype_2 = genotype_2, genotype_1

    alternate_arr = alternate_base.split(',')
    if len(alternate_arr) == 1:
        alternate_arr = (
            [reference_base if genotype_1 == 0 or genotype_2 == 0 else alternate_arr[0]] +
            alternate_arr
        )
    partial_labels = [utils.partial_label_from(reference_base, alternate) for alternate in alternate_arr]
    gt21_label = utils.mix_two_partial_labels(partial_labels[0], partial_labels[1])
    gt21 = utils.base_change_enum_from(gt21_label)

    is_homo_reference = genotype_1 == 0 and genotype_2 == 0
    is_homo_variant = not is_homo_reference and genotype_1 == genotype_2
    is_hetero_variant = not is_homo_reference and not is_homo_variant
    is_multi = not is_homo_variant and genotype_1 != 0 and genotype_2 != 0
    genotype = Genotype.unknown
    if is_homo_reference:
        genotype = Genotype.homo_reference
    elif is_homo_variant:
        genotype = Genotype.homo_variant
    elif is_hetero_variant and not is_multi:
        genotype = Genotype.hetero_variant
    elif is_hetero_variant and is_multi:
        genotype = Genotype.hetero_variant
        # genotype = Genotype.hetero_variant_multi

    if genotype == Genotype.unknown:
        return 0
    tmp = (-10 * log(e, 10)) * (
        log(
            ((1.0 - gt21_probabilities[gt21] * genotype_probabilities[genotype]) + 1e-300) /
            (gt21_probabilities[gt21] * genotype_probabilities[genotype] + 1e-300)
        )
    ) + 33

    return int(round(tmp * tmp))


def Output(
    args,
    call_fh,
    batch_size,
    X,
    batch_chr_pos_seq,
    batch_base_change_probabilities,
    batch_genotype_probabilities,
    batch_variant_length_probabilities_1,
    batch_variant_length_probabilities_2,
    sam_file,
    fasta_file
):
    if len(batch_base_change_probabilities) != batch_size:
        sys.exit(
            "Inconsistent shape between input tensor and output predictions %d/%d" %
            (batch_size, len(batch_base_change_probabilities))
        )

    is_show_reference = args.showRef
    position_center = flanking_base_number
    is_debug = True if args.debug is True else False
    is_using_pysam_for_all_indel_bases_output = args.pysam_for_all_indel_bases

    for (
        x,
        chr_pos_seq,
        gt21_probabilities,
        genotype_probabilities,
        variant_length_probabilities_1,
        variant_length_probabilities_2
    ) in izip(
        X,
        batch_chr_pos_seq,
        batch_base_change_probabilities,
        batch_genotype_probabilities,
        batch_variant_length_probabilities_1,
        batch_variant_length_probabilities_2
    ):
        # get chromosome, position and reference bases
        # with flanking "flanking_base_number" flanking bases at position
        chromosome, position, reference_sequence = chr_pos_seq.split(":")
        position = int(position)

        # calculate all possible variant cases probabilities for comparison
        homo_reference_probability = genotype_probabilities[Genotype.homo_reference]
        homo_variant_probability = genotype_probabilities[Genotype.homo_variant]
        hetero_variant_probability = genotype_probabilities[Genotype.hetero_variant]
        zero_variant_length_probability = (
            variant_length_probabilities_1[0 + VariantLength.index_offset] *
            variant_length_probabilities_2[0 + VariantLength.index_offset]
        )
        insert_length, _, homo_insert_variant_length_probability = insertion_length_tuple_from(
            variant_length_probabilities_1, variant_length_probabilities_2
        )
        delete_length, _, homo_delete_variant_length_probability = deletion_length_tuple_from(
            variant_length_probabilities_1, variant_length_probabilities_2
        )
        _hetero_ACGT_Ins_length_1, hetero_ACGT_Ins_length_2, hetero_ACGT_Ins_variant_length_probability = (
            insertion_length_tuple_from(
                variant_length_probabilities_1, variant_length_probabilities_2, is_hetero_Ins=True
            )
        )
        hetero_InsIns_length_1, hetero_InsIns_length_2, hetero_InsIns_variant_length_probability = (
            insertion_length_tuple_from(
                variant_length_probabilities_1, variant_length_probabilities_2, is_hetero_InsIns=True
            )
        )
        _hetero_ACGT_Del_length_1, hetero_ACGT_Del_length_2, hetero_ACGT_Del_variant_length_probability = (
            deletion_length_tuple_from(
                variant_length_probabilities_1, variant_length_probabilities_2, is_hetero_Del=True
            )
        )
        hetero_DelDel_length_1, hetero_DelDel_length_2, hetero_DelDel_variant_length_probability = (
            deletion_length_tuple_from(
                variant_length_probabilities_1, variant_length_probabilities_2, is_hetero_DelDel=True
            )
        )
        hetero_InsDel_length_1, hetero_InsDel_length_2, hetero_InsDel_variant_length_probability = (
            insertion_and_deletion_length_tuple_from(variant_length_probabilities_1, variant_length_probabilities_2)
        )

        reference_base = reference_sequence[position_center]
        reference_base_change = utils.base_change_enum_from(reference_base+reference_base)
        homo_Ref_probability = (
            gt21_probabilities[reference_base_change] * zero_variant_length_probability * homo_reference_probability
        )
        homo_SNP_probability = max(
            gt21_probabilities[GT21.AA],
            gt21_probabilities[GT21.CC],
            gt21_probabilities[GT21.GG],
            gt21_probabilities[GT21.TT],
        ) * zero_variant_length_probability * homo_variant_probability
        hetero_SNP_probability = max(
            gt21_probabilities[GT21.AC],
            gt21_probabilities[GT21.AG],
            gt21_probabilities[GT21.AT],
            gt21_probabilities[GT21.CG],
            gt21_probabilities[GT21.CT],
            gt21_probabilities[GT21.GT],
        ) * zero_variant_length_probability * hetero_variant_probability
        homo_insert_probability = (
            gt21_probabilities[GT21.InsIns] * homo_insert_variant_length_probability * homo_variant_probability
        )
        homo_delete_probability = (
            gt21_probabilities[GT21.DelDel] * homo_delete_variant_length_probability * homo_variant_probability
        )
        hetero_ACGT_Ins_probability = max(
            gt21_probabilities[GT21.AIns],
            gt21_probabilities[GT21.CIns],
            gt21_probabilities[GT21.GIns],
            gt21_probabilities[GT21.TIns],
        ) * hetero_ACGT_Ins_variant_length_probability * hetero_variant_probability
        hetero_InsIns_probability = (
            gt21_probabilities[GT21.InsIns] * hetero_InsIns_variant_length_probability * hetero_variant_probability
        )
        hetero_ACGT_Del_probability = max(
            gt21_probabilities[GT21.ADel],
            gt21_probabilities[GT21.CDel],
            gt21_probabilities[GT21.GDel],
            gt21_probabilities[GT21.TDel],
        ) * hetero_ACGT_Del_variant_length_probability * hetero_variant_probability
        hetero_DelDel_probability = (
            gt21_probabilities[GT21.DelDel] * hetero_DelDel_variant_length_probability * hetero_variant_probability
        )
        hetero_InsDel_probability = (
            gt21_probabilities[GT21.InsDel] * hetero_InsDel_variant_length_probability * hetero_variant_probability
        )
        maximum_probability = max(
            homo_Ref_probability,
            homo_SNP_probability,
            hetero_SNP_probability,
            homo_insert_probability,
            homo_delete_probability,
            hetero_ACGT_Ins_probability,
            hetero_InsIns_probability,
            hetero_ACGT_Del_probability,
            hetero_DelDel_probability,
            hetero_InsDel_probability,
        )

        is_reference = maximum_probability == homo_Ref_probability
        if not is_debug and not is_show_reference and is_reference:
            continue

        is_homo_SNP = maximum_probability == homo_SNP_probability
        is_hetero_SNP = maximum_probability == hetero_SNP_probability
        is_homo_insertion = maximum_probability == homo_insert_probability
        is_hetero_ACGT_Ins = maximum_probability == hetero_ACGT_Ins_probability
        is_hetero_InsIns = maximum_probability == hetero_InsIns_probability
        is_homo_deletion = maximum_probability == homo_delete_probability
        is_hetero_ACGT_Del = maximum_probability == hetero_ACGT_Del_probability
        is_hetero_DelDel = maximum_probability == hetero_DelDel_probability
        is_insertion_and_deletion = maximum_probability == hetero_InsDel_probability

        is_SNP = is_homo_SNP or is_hetero_SNP
        is_insertion = is_homo_insertion or is_hetero_ACGT_Ins or is_hetero_InsIns
        is_deletion = is_homo_deletion or is_hetero_ACGT_Del or is_hetero_DelDel

        # Initialize other variables
        length_guess = 0
        info = []

        # read depth
        read_depth = sum(x[position_center, :, Channel.delete] + x[position_center, :, Channel.reference])
        if read_depth == 0:
            print_debug_message_with(
                is_debug,
                call_fh,
                chromosome,
                position,
                gt21_probabilities,
                genotype_probabilities,
                variant_length_probabilities_1,
                variant_length_probabilities_2,
                "Read Depth is zero"
            )
            continue

        # geno type string, would changed to 1/2 later if is multi
        if is_reference:
            genotype_string = genotype_string_from(Genotype.homo_reference)
        elif is_homo_SNP or is_homo_insertion or is_homo_deletion:
            genotype_string = genotype_string_from(Genotype.homo_variant)
        elif is_hetero_SNP or is_hetero_ACGT_Ins or is_hetero_InsIns or is_hetero_ACGT_Del or is_hetero_DelDel:
            genotype_string = genotype_string_from(Genotype.hetero_variant)
        elif is_insertion_and_deletion:
            genotype_string = genotype_string_from(Genotype.hetero_variant_multi)

        # reference base and alternate base
        reference_base = ""
        alternate_base = ""
        if is_reference:
            reference_base = reference_sequence[position_center]
            alternate_base = reference_base

        elif is_homo_SNP:
            base1, base2 = homo_SNP_bases_from(gt21_probabilities)
            reference_base = reference_sequence[position_center]
            alternate_base = base1 if base1 != reference_base else base2

        elif is_hetero_SNP:
            base1, base2 = hetero_SNP_bases_from(gt21_probabilities)
            reference_base = reference_sequence[position_center]
            is_multi = base1 != reference_base and base2 != reference_base
            if is_multi:
                alternate_base = "{},{}".format(base1, base2)
                genotype_string = genotype_string_from(Genotype.hetero_variant_multi)
            else:
                alternate_base = base1 if base1 != reference_base else base2

        elif is_insertion:
            variant_length = 0
            if is_homo_insertion:
                variant_length = insert_length
            elif is_hetero_ACGT_Ins:
                variant_length = hetero_ACGT_Ins_length_2
            elif is_hetero_InsIns:
                variant_length = hetero_InsIns_length_2

            is_hetero_insertion = is_hetero_ACGT_Ins or is_hetero_InsIns
            if is_hetero_insertion and variant_length <= 0:
                print_debug_message_with(
                    is_debug,
                    call_fh,
                    chromosome,
                    position,
                    gt21_probabilities,
                    genotype_probabilities,
                    variant_length_probabilities_1,
                    variant_length_probabilities_2,
                    "is hetero insertion and # of insertion bases predicted is less than 0"
                )
                continue

            insertion_bases, insertion_length, is_inferred_insertion_bases = insertion_bases_from(
                tensor_input=x,
                variant_length=variant_length,
                sam_file=sam_file,
                contig=chromosome,
                position=position,
                is_using_pysam_for_all_indel_bases_output=is_using_pysam_for_all_indel_bases_output
            )
            if insertion_length > 0:
                reference_base = reference_sequence[position_center]
                alternate_base = reference_base + insertion_bases

            if is_inferred_insertion_bases:
                length_guess = insertion_length

            hetero_insert_base = hetero_insert_base_from(gt21_probabilities) if is_hetero_ACGT_Ins else ""
            is_SNP_Ins_multi = (
                is_hetero_ACGT_Ins and insertion_length > 0 and hetero_insert_base != reference_base
            )
            is_Ins_Ins_multi = is_hetero_InsIns and insertion_length > 0

            if is_SNP_Ins_multi:
                alternate_base = "{},{}".format(hetero_insert_base, alternate_base)
                genotype_string = genotype_string_from(Genotype.hetero_variant_multi)
            elif is_Ins_Ins_multi:
                another_insertion_bases = (
                    insertion_bases_using_pysam_from(
                        sam_file=sam_file,
                        contig=chromosome,
                        position=position,
                        minimum_insertion_length=hetero_InsIns_length_1,
                        maximum_insertion_length=maximum_variant_length_from(hetero_InsIns_length_1),
                        insertion_bases_to_ignore=insertion_bases
                    ) or
                    insertion_bases[0:hetero_InsIns_length_1]
                )
                alternate_base_1 = reference_base + another_insertion_bases
                alternate_base_2 = alternate_base
                if alternate_base_1 != alternate_base_2:
                    alternate_base = "{},{}".format(alternate_base_1, alternate_base_2)
                    genotype_string = genotype_string_from(Genotype.hetero_variant_multi)

        elif is_deletion:
            variant_length = 0
            if is_homo_deletion:
                variant_length = delete_length
            elif is_hetero_ACGT_Del:
                variant_length = hetero_ACGT_Del_length_2
            elif is_hetero_DelDel:
                variant_length = hetero_DelDel_length_2

            is_hetero_deletion = is_hetero_ACGT_Del or is_hetero_DelDel
            if is_hetero_deletion and variant_length <= 0:
                print_debug_message_with(
                    is_debug,
                    call_fh,
                    chromosome,
                    position,
                    gt21_probabilities,
                    genotype_probabilities,
                    variant_length_probabilities_1,
                    variant_length_probabilities_2,
                    "is hetero deletion and # of deletion bases predicted is less than 0"
                )
                continue

            deletion_bases, deletion_length, is_inferred_deletion_bases = deletion_bases_from(
                tensor_input=x,
                variant_length=variant_length,
                sam_file=sam_file,
                fasta_file=fasta_file,
                contig=chromosome,
                position=position,
                reference_sequence=reference_sequence,
                is_using_pysam_for_all_indel_bases_output=is_using_pysam_for_all_indel_bases_output
            )
            if deletion_length > 0:
                reference_base = reference_sequence[position_center] + deletion_bases
                alternate_base = reference_base[0]

            if is_inferred_deletion_bases:
                length_guess = deletion_length

            hetero_delete_base = hetero_delete_base_from(gt21_probabilities) if is_hetero_ACGT_Del else ""
            is_SNP_Del_multi = (
                is_hetero_ACGT_Del and deletion_length > 0 and hetero_delete_base != reference_base[0]
            )
            is_Del_Del_multi = is_hetero_DelDel and deletion_length > 0

            if is_SNP_Del_multi:
                alternate_base_1 = alternate_base
                alternate_base_2 = hetero_delete_base + reference_base[1:]
                alternate_base = "{},{}".format(alternate_base_1, alternate_base_2)
                genotype_string = genotype_string_from(Genotype.hetero_variant_multi)
            elif is_Del_Del_multi:
                alternate_base_1 = alternate_base
                alternate_base_2 = reference_base[0] + reference_base[hetero_DelDel_length_1 + 1:]
                if (
                    alternate_base_1 != alternate_base_2 and
                    reference_base != alternate_base_1 and
                    reference_base != alternate_base_2
                ):
                    alternate_base = "{},{}".format(alternate_base_1, alternate_base_2)
                    genotype_string = genotype_string_from(Genotype.hetero_variant_multi)

        elif is_insertion_and_deletion:
            insertion_bases, insertion_length, _ = insertion_bases_from(
                tensor_input=x,
                variant_length=hetero_InsDel_length_2,
                sam_file=sam_file,
                contig=chromosome,
                position=position,
                is_using_pysam_for_all_indel_bases_output=is_using_pysam_for_all_indel_bases_output
            )
            deletion_bases, deletion_length, _ = deletion_bases_from(
                tensor_input=x,
                variant_length=hetero_InsDel_length_1,
                sam_file=sam_file,
                fasta_file=fasta_file,
                contig=chromosome,
                position=position,
                reference_sequence=reference_sequence,
                is_using_pysam_for_all_indel_bases_output=is_using_pysam_for_all_indel_bases_output
            )

            if insertion_length > 0 and deletion_length > 0:
                reference_base = reference_sequence[position_center] + deletion_bases
                alternate_base = "{},{}".format(
                    reference_base[0],
                    reference_base[0] + insertion_bases + reference_base[1:]
                )

        if reference_base == "" or alternate_base == "":
            print_debug_message_with(
                is_debug,
                call_fh,
                chromosome,
                position,
                gt21_probabilities,
                genotype_probabilities,
                variant_length_probabilities_1,
                variant_length_probabilities_2,
                "no reference base / alternate base prediction"
            )
            continue

        # allele frequency / supported reads
        supported_reads_count = 0
        if is_reference:
            supported_reads_count = (
                x[position_center,   base2num[reference_base], Channel.reference] +
                x[position_center, base2num[reference_base]+4, Channel.reference]
            )
        elif is_SNP:
            for base in alternate_base:
                if base == ',':
                    continue
                supported_reads_count += (
                    x[position_center,   base2num[base], Channel.SNP] +
                    x[position_center, base2num[base]+4, Channel.SNP] +
                    x[position_center,   base2num[base], Channel.reference] +
                    x[position_center, base2num[base]+4, Channel.reference]
                )
        elif is_insertion:
            supported_reads_count = (
                sum(x[position_center+1, :, Channel.insert]) -
                sum(x[position_center+1, :, Channel.SNP])
            )
        elif is_deletion:
            supported_reads_count = sum(x[position_center+1, :, Channel.delete])
        elif is_insertion_and_deletion:
            supported_reads_count = (
                sum(x[position_center+1, :, Channel.insert]) +
                sum(x[position_center+1, :, Channel.delete]) -
                sum(x[position_center+1, :, Channel.SNP])
            )
        allele_frequency = ((supported_reads_count + 0.0) / read_depth) if read_depth != 0 else 0.0
        if allele_frequency > 1:
            allele_frequency = 1

        # if using inferred indel length, add info LENGUESS
        if 0 < length_guess < flanking_base_number:
            info.append("LENGUESS={}".format(length_guess))

        # information string
        information_string = ""
        if len(info) == 0:
            information_string = "."
        else:
            information_string = ";".join(info)

        # quality score
        quality_score = quality_score_from(
            reference_base,
            alternate_base,
            genotype_string,
            gt21_probabilities,
            genotype_probabilities,
        )

        # filtration value
        filtration_value = filtration_value_from(quality_score_for_pass=args.qual, quality_score=quality_score)

        if is_debug:
            print_debug_message_with(
                is_debug,
                call_fh,
                chromosome,
                position,
                gt21_probabilities,
                genotype_probabilities,
                variant_length_probabilities_1,
                variant_length_probabilities_2,
                "Normal output" if not is_reference else "Reference"
            )
        else:
            print >> call_fh, "%s\t%d\t.\t%s\t%s\t%d\t%s\t%s\tGT:GQ:DP:AF\t%s:%d:%d:%.4f" % (
                chromosome,
                position,
                reference_base,
                alternate_base,
                quality_score,
                filtration_value,
                information_string,
                genotype_string,
                quality_score,
                read_depth,
                allele_frequency
            )


def print_vcf_header(args, call_fh):
    print >> call_fh, '##fileformat=VCFv4.1'
    print >> call_fh, '##FILTER=<ID=PASS,Description="All filters passed">'
    print >> call_fh, '##FILTER=<ID=LowQual,Description="Confidence in this variant being real is below calling threshold.">'
    print >> call_fh, '##ALT=<ID=DEL,Description="Deletion">'
    print >> call_fh, '##ALT=<ID=INS,Description="Insertion of novel sequence">'
    print >> call_fh, '##INFO=<ID=SVTYPE,Number=1,Type=String,Description="Type of structural variant">'
    print >> call_fh, '##INFO=<ID=LENGUESS,Number=.,Type=Integer,Description="Best guess of the indel length">'
    print >> call_fh, '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">'
    print >> call_fh, '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype Quality">'
    print >> call_fh, '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Read Depth">'
    print >> call_fh, '##FORMAT=<ID=AF,Number=1,Type=Float,Description="Estimated allele frequency in the range (0,1)">'

    if args.ref_fn != None:
        fai_fn = args.ref_fn + ".fai"
        fai_fp = open(fai_fn)
        for line in fai_fp:
            fields = line.strip().split("\t")
            chromName = fields[0]
            chromLength = int(fields[1])
            print >> call_fh, "##contig=<ID=%s,length=%d>" % (chromName, chromLength)

    print >> call_fh, '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t%s' % (args.sampleName)


def log_activation(args, m, utils):
    if args.log_path is None:
        return

    summary_writer = m.get_summary_file_writer(args.log_path)

    if summary_writer is None:
        return

    tensorGenerator = utils.GetTensor(args.tensor_fn, param.predictBatchSize)
    logging.info("Plotting activations ...")

    num_plotted = 0
    while(num_plotted < args.max_plot or args.max_plot < 0):
        print("Getting next batch")
        is_end_of_generator, batch_size, batch_X, batch_positions = next(tensorGenerator)
        print("Batch generation complete %d" % batch_size)
        # strip away the reference string, keeping the chr and coor only
        batch_positions = [s[:s.rfind(":")] for s in batch_positions]
        summaries = m.get_activation_summary(
            batch_X,
            operations=m.layers,
            batch_item_suffixes=batch_positions,
            max_plot_in_batch=args.max_plot - num_plotted if args.max_plot >= 0 else batch_size,
            parallel_level=args.parallel_level,
            num_workers=args.workers,
            fast_plotting=args.fast_plotting
        )
        for summary in summaries:
            summary_writer.add_summary(summary)
        num_plotted += min(batch_size, args.max_plot - num_plotted if args.max_plot >= 0 else batch_size)
        if is_end_of_generator == 1:
            break
    print("Finished plotting %d" % num_plotted)


def Test(args, m, utils):
    call_fh = open(args.call_fn, "w")
    fasta_file = pysam.FastaFile(filename=args.ref_fn) if args.ref_fn else None
    sam_file = pysam.AlignmentFile(args.bam_fn, mode="rb")

    print_vcf_header(args, call_fh)

    tensorGenerator = utils.GetTensor(args.tensor_fn, param.predictBatchSize)
    logging.info("Calling variants ...")
    predictStart = time.time()
    end = 0
    end2 = 0
    terminate = 0
    end2, num2, XBatch2, posBatch2 = next(tensorGenerator)
    m.predict(XBatch2, result_caching=True)
    base = m.predictBaseRTVal
    gt = m.predictGenotypeRTVal
    l1 = m.predictIndelLengthRTVal1
    l2 = m.predictIndelLengthRTVal2
    if end2 == 0:
        end = end2
        num = num2
        XBatch = XBatch2
        posBatch = posBatch2
        end2, num2, XBatch2, posBatch2 = next(tensorGenerator)
        while True:
            if end == 1:
                terminate = 1
            threadPool = []
            if end == 0:
                threadPool.append(Thread(target=m.predict, args=(XBatch2, True)))
            threadPool.append(
                Thread(
                    target=Output,
                    args=(args, call_fh, num, XBatch, posBatch, base, gt, l1, l2, sam_file, fasta_file)
                )
            )
            for t in threadPool:
                t.start()
            if end2 == 0:
                end3, num3, XBatch3, posBatch3 = next(tensorGenerator)
            for t in threadPool:
                t.join()
            base = m.predictBaseRTVal
            gt = m.predictGenotypeRTVal
            l1 = m.predictIndelLengthRTVal1
            l2 = m.predictIndelLengthRTVal2
            if end == 0:
                end = end2
                num = num2
                XBatch = XBatch2
                posBatch = posBatch2
            if end2 == 0:
                end2 = end3
                num2 = num3
                XBatch2 = XBatch3
                posBatch2 = posBatch3
            # print >> sys.stderr, end, end2, end3, terminate
            if terminate == 1:
                break
    elif end2 == 1:
        Output(args, call_fh, num2, XBatch2, posBatch2, base, gt, l1, l2, sam_file, fasta_file)

    logging.info("Total time elapsed: %.2f s" % (time.time() - predictStart))

    sam_file.close()
    fasta_file.close()
    call_fh.close()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Call variants using a trained Clair model and tensors of candididate variants")

    parser.add_argument('--tensor_fn', type=str, default="PIPE",
                        help="Tensor input, use PIPE for standard input")

    parser.add_argument('--chkpnt_fn', type=str, default=None,
                        help="Input a checkpoint for testing or continue training")

    parser.add_argument('--call_fn', type=str, default=None,
                        help="Output variant predictions")

    parser.add_argument('--bam_fn', type=str, default="bam.bam",
                        help="BAM file input, default: %(default)s")

    parser.add_argument('--qual', type=int, default=None,
                        help="If set, variant with equal or higher quality will be marked PASS, or LowQual otherwise, optional")

    parser.add_argument('--sampleName', type=str, default="SAMPLE",
                        help="Define the sample name to be shown in the VCF file")

    parser.add_argument('--showRef', action='store_true',
                        help="Show reference calls, optional")

    parser.add_argument('--debug', action='store_true',
                        help="Debug mode, optional")

    parser.add_argument('--ref_fn', type=str, default=None,
                        help="Reference fasta file input, optional, print contig tags in the VCF header if set")

    parser.add_argument('--threads', type=int, default=None,
                        help="Number of threads, optional")

    parser.add_argument('--activation_only', action='store_true',
                        help="Output activation only, no prediction")

    parser.add_argument('--max_plot', type=int, default=10,
                        help="The maximum number of plots output, negative number means no limit (plot all), default: %(default)s")

    parser.add_argument('--log_path', type=str, nargs='?', default=None,
                        help="The path for tensorflow logging, default: %(default)s")

    parser.add_argument('-p', '--parallel_level', type=int, default=2,
                        help="The level of parallelism in plotting (currently available: 0, 2), default: %(default)s")

    parser.add_argument('--fast_plotting', action='store_true',
                        help="Enable fast plotting.")

    parser.add_argument('-w', '--workers', type=int, default=8,
                        help="The number of workers in plotting, default: %(default)s")

    parser.add_argument('--pysam_for_all_indel_bases', action='store_true',
                        help="Always using pysam for outputting indel bases, optional")

    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit(1)

    Run(args)

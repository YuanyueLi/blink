#!/usr/bin/env python

import sys
import os
import argparse
import glob
from timeit import default_timer as timer
import logging

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import norm
import pandas as pd
from pyteomics import mgf

###########################
# Mass Spectra Transforms
###########################

def remove_duplicate_ions(mzis, min_diff=0.002):
    """
    remove peaks from a list of 2xM mass spectrum vectors (mzis) that are within min_diff
    by averaging m/zs and summing intensities to remove duplicates

    options:
        min_diff, float
            minimum difference possible given,
            a good rule of thumb is ions have to be greater than 2 bins apart

    returns:
        mzis, list of 2xM mass spectrum vectors
    """
    bad_ones = [i for i,s in enumerate(mzis) if min(np.diff(s[0],prepend=0))<=min_diff]
    for bad_idx in sorted(bad_ones, reverse=True):
        idx = np.argwhere(np.diff(mzis[bad_idx][0])<min_diff).flatten()
        idx = sorted(idx,reverse=True)
        for sub_idx in idx:
            dup_mz = mzis[bad_idx][0][sub_idx:sub_idx+2]
            dup_intensities = mzis[bad_idx][1][sub_idx:sub_idx+2]
            new_mz = np.mean(dup_mz)
            new_intensity = np.sum(dup_intensities)
            mzis[bad_idx][0][sub_idx:sub_idx+2] = new_mz
            mzis[bad_idx][1][sub_idx:sub_idx+2] = new_intensity
        mz = np.delete(mzis[bad_idx][0],idx)
        intensity = np.delete(mzis[bad_idx][1],idx)
        mzis[bad_idx] = np.asarray([mz,intensity])

    return mzis

def discretize_spectra(mzis, pmzs, bin_width=0.001, intensity_power=0.5, trim_empty=False, remove_duplicates=False,metadata=None):
    """
    converts a list of 2xM mass spectrum vectors (mzis) and pmzs into a dict-based sparse matrix of [mz/nl][i/c] components

    options:
        bin_width, float
            width of bin to use in mz
        intensity_power, float
            power to raise intensity to before normalizing
        remove_duplicates, bool
            average mz and intensity over peaks within 2 times bin_width

    returns:
        {'ic',
         'spec_ids',
         'mz',
         'pmz',
         'bin_width',
         'intensity_power'}
    """
    if trim_empty:
        kept, mzis = np.array([[idx,mzi] for idx,mzi
                                in enumerate(mzis)
                                if mzi.size>0], dtype=object).T
    if remove_duplicates:
        mzis = remove_duplicate_ions(mzis,min_diff=bin_width*2)

    spec_ids = np.concatenate([[i]*mzi.shape[1] for i,mzi in enumerate(mzis)]).astype(int)

    inorm = np.array([1./np.linalg.norm(mzi[1]**intensity_power) for mzi in mzis])
    cnorm = np.array([mzi.shape[1]**.5/np.linalg.norm(np.ones_like(mzi[1])) for mzi in mzis])

    mzis = np.concatenate(mzis, axis=1)
    mzis[1] = mzis[1]**intensity_power
    mz_bin_idxs = np.rint(mzis[0]/bin_width).astype(complex)
    nl_bin_idxs = np.rint(np.asarray(pmzs)[spec_ids]/bin_width) - mz_bin_idxs
    mz_bin_idxs = mz_bin_idxs + nl_bin_idxs*(0+1j)

    shift = -mz_bin_idxs.imag.min().astype(int)

    # Convert binned mzs/nls and normalized intensities/counts into coordinate list format
    ic =  sp.coo_matrix((np.concatenate([inorm[spec_ids]*mzis[1], cnorm[spec_ids]*(0+1j)]),
                        (np.concatenate([spec_ids, spec_ids]),
                         np.concatenate([mz_bin_idxs.real.astype(int)+shift,
                                         mz_bin_idxs.imag.astype(int)+shift]))),
                         dtype=complex)

    S = {'ic': ic.data,
         'spec_ids' : ic.row,
         'mz': ic.col,
         'pmz': pmzs,
         'shift':shift,
         'bin_width': bin_width,
         'intensity_power': intensity_power,
        'metadata':metadata}
    
    if trim_empty:
        S['blanks'] = np.setdiff1d(np.arange(spec_ids[-1]+1),kept)

    return S


##########
# Kernel
##########
def network_kernel(S, tolerance=0.01, mass_diffs=[0], react_steps=1):
    """
    apply network kernel to all mzs/nls in S that are within
    tolerance of any combination of mass_diffs within react_steps

    options:
        tolerance, float
            tolerance in mz from mass_diffs for networking ions
        mass_diffs, listlike of floats
            mass differences to consider networking ions
        react_steps, int
            expand mass_diffs by the +/- combination of all mass_diffs within
            specified number of reaction steps

    returns:
        S + {'intensity_net',
             'count_net',
             'spec_ids_net',
             'mz_net'}
    """
    bin_num = int(2*(tolerance/S['bin_width'])-1)

    mass_diffs = np.sort(np.abs(mass_diffs))

    mass_diffs = [-m for m in mass_diffs[::-1]]+[m for m in mass_diffs]
    if mass_diffs[len(mass_diffs)//2] == 0:
        mass_diffs.pop(len(mass_diffs)//2)

    mass_diffs = np.rint(np.array(mass_diffs)/S['bin_width']).astype(int)

    # Recursively "react" mass_diffs within a specified number of reation steps
    def react(mass_diffs, react_steps):
        if react_steps == 1:
            return mass_diffs
        else:
            return np.add.outer(mass_diffs, react(mass_diffs, react_steps-1))

    # Expand reacted mass_diffs to have a tolerance
    mass_diffs = np.unique(np.sort(react(mass_diffs, react_steps).flatten()))
    mass_diffs = np.add.outer(mass_diffs, np.arange(-bin_num//2+1, bin_num//2+1)).flatten()

    # Apply kernel by outer summing and flattening low-level sparse matrix data structure
    S['ic_net'] = np.add.outer(S['ic'], np.zeros_like(mass_diffs, dtype=complex)).flatten()
    S['spec_ids_net'] = np.add.outer(S['spec_ids'], np.zeros_like(mass_diffs)).flatten()
    S['mz_net'] =  np.add.outer(S['mz'], mass_diffs).flatten()
    S['shift_net'] = S['shift']-S['mz_net'].min()
    S['mz_net'] -= S['mz_net'].min()

    return S


#####################
# Biochemical Masses
#####################

biochem_masses = [0.,      # Self
                  12.,     # C
                  1.00783, # H
                  2.01566, # H2
                  15.99491,# O
                  0.02381, # NH2 - O
                  78.95851,# PO3
                  31.97207]# S


############################
# Comparing Sparse Spectra
############################

def score_sparse_spectra(query, ref, tolerance=0.01, mass_diffs=[0], react_steps=1):
    """
    score/match/compare two sparse mass spectra

    tolerance, float
        tolerance in mz from mass_diffs for networking ions
    mass_diffs, listlike of floats
        mass differences to consider networking ions
    react_steps, int
        expand mass_diffs by the +/- combination of all mass_diffs within
        specified number of reaction steps

    returns:
        query vs ref scores, scipy.sparse.csr_matrix
    """
    
    def expand_sparse_spectra(spectra, shape=None, networked=False):
        # Expand complex valued sparse matrices into [mz/nl][i/c] matrices
        if networked:
            networked = '_net'
        else:
            networked = ''

        mz = spectra['mz'+networked][spectra['ic'+networked].real>0]
        nl = spectra['mz'+networked][spectra['ic'+networked].imag>0]
        mz_spec_ids = spectra['spec_ids'+networked][spectra['ic'+networked].real>0]
        nl_spec_ids = spectra['spec_ids'+networked][spectra['ic'+networked].imag>0]
        i =  spectra['ic'+networked].real[spectra['ic'+networked].real>0]
        c =  spectra['ic'+networked].imag[spectra['ic'+networked].imag>0]

        expanded_spectra = {'mzi': sp.coo_matrix((i, (mz, mz_spec_ids)), dtype=float, copy=False),
             'nli': sp.coo_matrix((i, (nl, nl_spec_ids)), dtype=float, copy=False),
             'mzc': sp.coo_matrix((c, (mz, mz_spec_ids)), dtype=int,   copy=False),
             'nlc': sp.coo_matrix((c, (nl, nl_spec_ids)), dtype=int,   copy=False)}

        return expanded_spectra
    
    ordered = ref['ic'].size < query['ic'].size
    
    network_kernel(query if ordered else ref, tolerance, mass_diffs, react_steps)
    
    e_q = expand_sparse_spectra(query, networked=ordered)
    e_r = expand_sparse_spectra(ref, networked=not ordered)

    query_shift = query['shift_net'] if ordered else query['shift']
    ref_shift = ref['shift'] if ordered else ref['shift_net']

    # Return score/matches matrices for mzs/nls
    query_vs_ref = {}
    for k in set(e_q.keys()) & set(e_r.keys()):
        vq, vr = e_q[k].T, e_r[k]

        if query_shift < ref_shift:
            vq = sp.hstack([sp.coo_matrix((vq.shape[0], ref_shift-query_shift), dtype=vq.dtype), vq], format='csr', dtype=vq.dtype)
        if ref_shift < query_shift:
            vr = sp.vstack([sp.coo_matrix((query_shift-ref_shift, vr.shape[1]), dtype=vr.dtype), vr], format='csc', dtype=vr.dtype)

        max_mz = max(vq.shape[1],vr.shape[0])
        vq.resize((vq.shape[0],max_mz))
        vr.resize((max_mz,vr.shape[1]))

        query_vs_ref[k] = vq.tocsr().dot(vr.tocsc()) #will set query as rows and ref as columns

    return query_vs_ref

#######################
# Mass Spectra Loading
#######################

def read_mgf(in_file):
    msms_df = []
    with mgf.MGF(in_file) as reader:
        for spectrum in reader:
            d = spectrum['params']
            d['spectrum'] = np.array([spectrum['m/z array'],
                                      spectrum['intensity array']])
            d['precursor_mz'] = d['pepmass'][0]
            msms_df.append(d)
    msms_df = pd.DataFrame(msms_df)
    return msms_df

def write_sparse_msms_file(out_file, S):
    np.savez_compressed(out_file, **S)

def open_msms_file(in_file):
    if '.mgf' in in_file:
        logging.info('Processing {}'.format(os.path.basename(in_file)))
        return read_mgf(in_file)
    else:
        logging.error('Unsupported file type: {}'.format(os.path.splitext(in_file)[-1]))
        raise IOError

def open_sparse_msms_file(in_file):
    if '.npz' in in_file:
        logging.info('Processing {}'.format(os.path.basename(in_file)))
        with np.load(in_file, mmap_mode='w+',allow_pickle=True) as S:
            return dict(S)
    else:
        logging.error('Unsupported file type: {}'.format(os.path.splitext(in_file)[-1]))
        raise IOError


#########################
# Command Line Interface
#########################

'''
https://stackoverflow.com/questions/4194948/python-argparse-is-there-a-way-to-specify-a-range-in-nargs
unutbu
'''
def required_length(nmin,nmax):
    class RequiredLength(argparse.Action):
        def __call__(self, parser, args, values, option_string=None):
            if not nmin<=len(values)<=nmax:
                msg='argument "{f}" requires between {nmin} and {nmax} arguments'.format(
                    f=self.dest,nmin=nmin,nmax=nmax)
                raise argparse.ArgumentTypeError(msg)
            setattr(args, self.dest, values)
    return RequiredLength

def arg_parser(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser(description='BLINK discretizes mass spectra (given .mgf inputs), and scores discretized spectra (given .npz inputs)')

    parser.add_argument('files',nargs='+', action=required_length(1,2), metavar='F', help='files to process')

    #Discretize options
    discretize_options = parser.add_argument_group()
    discretize_options.add_argument('--trim', action='store_true', default=False, required=False,
                                    help='remove empty spectra when discretizing')
    discretize_options.add_argument('--dedup', action='store_true', default=False, required=False,
                                    help='deduplicate fragment ions within 2 times bin_width')
    discretize_options.add_argument('-b','--bin_width', type=float, metavar='B', default=.001, required=False,
                                 help='width of bins in mz')
    discretize_options.add_argument('-i','--intensity_power', type=float, metavar='I', default=.5, required=False,
                                 help='power to raise intensites to in when scoring')

    #Compute options
    compute_options = parser.add_argument_group()
    compute_options.add_argument('-t','--tolerance', type=float, metavar='T', default=.01, required=False,
                                 help='maximum tolerance in mz for fragment ions to match')
    compute_options.add_argument('-d','--mass_diffs', type=float, metavar='D', nargs='*', default=[0], required=False,
                              help='mass diffs to network')
    compute_options.add_argument('-r','--react_steps', type=int, metavar='R', default=1, required=False,
                              help='recursively combine mass_diffs within number of reaction steps')
    compute_options.add_argument('-s','--min_score', type=float, default=.4, metavar='S', required=False,
                                 help='minimum score to include in output')
    compute_options.add_argument('-m','--min_matches', type=int, default=3, metavar='M', required=False,
                                 help='minimum matches to include in output')

    #Output file options
    output_options = parser.add_argument_group()
    output_options.add_argument('--fast_format', action='store_true', default=False, required=False,
                                help='use fast .npz format to store scores instead of .tab')
    output_options.add_argument('-f', '--force', action='store_true', required=False,
                                help='force file(s) to be remade if they exist')
    output_options.add_argument('-o','--out_dir', type=str, metavar='O', required=False,
                                help='change output location for output file(s)')

    return parser

def main():
    parser = arg_parser()
    args = parser.parse_args()

    logging.basicConfig(filename=os.path.join(os.getcwd(),'blink.log'), level=logging.INFO)

    common_ext = {os.path.splitext(in_file)[1]
                  for f in args.files
                  for in_file in glob.glob(f)}
    if len(common_ext) == 1:
        common_ext = list(common_ext)[0]

    if common_ext == '.mgf':
        logging.info('Discretize Start')

        files = [os.path.splitext(os.path.splitext(
                 os.path.basename(in_file))[0])[0]
                 for input in args.files
                 for in_file in glob.glob(input)]

        prefix = os.path.commonprefix(files)

        if len(files) > 1:
            out_name = '-'.join([files[0],files[-1]])
        else:
            out_name = files[0]

        if args.out_dir:
            out_dir = args.out_dir
        else:
            out_dir = os.path.dirname(os.path.abspath(glob.glob(args.files[0])[0]))

        logging.info('Output to {}'.format(out_dir))
        out_loc = os.path.join(out_dir, out_name+'.npz')

        if not args.force and os.path.isfile(out_loc):
            logging.info('{} already exists. Skipping.'.format(out_name))
            logging.info('Discretize End')
            sys.exit(0)

        dense_spectra =[open_msms_file(ff)[['spectrum','precursor_mz']]
                        for f in args.files
                        for ff in glob.glob(f)]
        file_ids = np.cumsum(np.array([s.spectrum.shape[0] for s in dense_spectra]))
        pmzs = np.concatenate([s.precursor_mz for s in dense_spectra]).tolist()
        dense_spectra = np.concatenate([s.spectrum for s in dense_spectra])

        start = timer()
        S = discretize_spectra(dense_spectra,pmzs=pmzs,bin_width=args.bin_width,
                               intensity_power=args.intensity_power,
                               trim_empty=args.trim,remove_duplicates=args.dedup)
        end = timer()

        S['file_ids'] = file_ids

        write_sparse_msms_file(out_loc, S)

        logging.info('Discretize Time: {} seconds, {} spectra'.format(end-start, S['spec_ids'].max()+1))
        logging.info('Discretize End')

    elif common_ext == '.npz':
        logging.info('Score Start')

        out_name = '_'.join([os.path.splitext(os.path.splitext(os.path.basename(f))[0])[0] for f in args.files])

        if args.out_dir:
            out_dir = args.out_dir
        else:
            out_dir = os.path.dirname(os.path.abspath(args.files[0]))
        logging.info('Output to {}'.format(out_dir))

        out_loc = os.path.join(out_dir, out_name)

        if not args.force and os.path.isfile(out_loc):
            logging.info('{} already exists. Skipping.'.format(out_name))
            logging.info('Score End')
            sys.exit(0)

        S1 = open_sparse_msms_file(args.files[0])
        bin_width = S1['bin_width']
        S1_blanks = S1.get('blanks',np.array([]))

        if len(args.files) == 1:
            S2 = S1
            S2_blanks = S1_blanks
        else:
            S2 = open_sparse_msms_file(args.files[1])
            S2_blanks = S2.get('blanks',np.array([]))

            try:
                assert S2['bin_width'] == bin_width
            except AssertionError:
                log.error('Input files have differing bin_width')
                sys.exit(1)

        start = timer()
        S12 = score_sparse_spectra(S1, S2,
                                   mass_diffs=args.mass_diffs,
                                   react_steps=args.react_steps,
                                   tolerance=args.tolerance)
        end = timer()
        logging.info('Score Time: {} seconds'.format(end-start))

        if (args.min_score > 0) or (args.min_matches > 0):
            logging.info('Filtering')
            keep_idx =  S12['mzi'] >= args.min_score
            keep_idx = keep_idx.maximum(S12['mzc'] >= args.min_matches)
            if 'nli' in S12.keys():
                keep_idx = keep_idx.maximum(S12['nli'] >= args.min_score)
            if 'nlc' in S12.keys():
                keep_idx = keep_idx.maximum(S12['nlc'] >= args.min_matches)

            for k in S12.keys():
                S12[k] = S12[k].multiply(keep_idx).tocoo()
        else:
            for k in S12.keys():
                S12[k] = S12[k].tocoo()

        if args.fast_format:
            write_sparse_msms_file(out_loc+'_scores.npz', S12)
        else:
            out_df = pd.concat([pd.Series(S12[k].data, name=k,
                                          index=list(zip(S12[k].col.tolist(),
                                                         S12[k].row.tolist())))
                                for k in S12.keys()], axis=1)

            out_df.index.names = ['/'.join([str(args.tolerance),
                                            ','.join([str(d) for d in args.mass_diffs]),
                                            str(args.react_steps),
                                            str(args.min_score),
                                            str(args.min_matches)]),'']

            out_df.to_csv(out_loc+'.tab', index=True, sep='\t', columns = sorted(out_df.columns,key=lambda c:c[::-1])[::-1])

        logging.info('Score End')

    else:
        logging.error('Input files must only be .mgf or .npz')
        sys.exit(1)

if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""
Created on Tue Sep 22 14:23:47 2015

@author: lpinello
"""



import os
import errno
import sys
import subprocess as sb
import glob
import gzip
import argparse
import unicodedata
import string
import re

import pandas as pd
import numpy as np
import multiprocessing


import logging
logging.basicConfig(level=logging.INFO,
                     format='%(levelname)-5s @ %(asctime)s:\n\t %(message)s \n',
                     datefmt='%a, %d %b %Y %H:%M:%S',
                     stream=sys.stderr,
                     filemode="w"
                     )
error   = logging.critical
warn    = logging.warning
debug   = logging.debug
info    = logging.info



_ROOT = os.path.abspath(os.path.dirname(__file__))

    
####Support functions###
def get_data(path):
        return os.path.join(_ROOT, 'data', path)

GENOME_LOCAL_FOLDER=get_data('genomes')

def force_symlink(src, dst):
    try:
        os.symlink(src, dst)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            os.remove(dst)
            os.symlink(src, dst)

nt_complement=dict({'A':'T','C':'G','G':'C','T':'A','N':'N','_':'_',})

def reverse_complement(seq):
        return "".join([nt_complement[c] for c in seq.upper()[-1::-1]])

def find_wrong_nt(sequence):
    return list(set(sequence.upper()).difference(set(['A','T','C','G','N'])))

def capitalize_sequence(x):
    return str(x).upper() if not pd.isnull(x) else x

def check_file(filename):
    try:
        with open(filename): pass
    except IOError:
        raise Exception('I cannot open the file: '+filename)

#the dependencies are bowtie2 and samtools
def which(program):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None


def check_samtools():

    cmd_path=which('samtools')
    if cmd_path:
        sys.stdout.write('\n samtools is installed! (%s)' %cmd_path)
        return True
    else:
        sys.stdout.write('\nCRISPRessoPooled requires samtools')
        sys.stdout.write('\n\nPlease install it and add to your path following the instruction at: http://www.htslib.org/download/')
        return False

def check_bowtie2():

    cmd_path1=which('bowtie2')
    cmd_path2=which('bowtie2-inspect')

    if cmd_path1 and cmd_path2:
        sys.stdout.write('\n bowtie2 is installed! (%s)' %cmd_path1)
        return True
    else:
        sys.stdout.write('\nCRISPRessoPooled requires Bowtie2!')
        sys.stdout.write('\n\nPlease install it and add to your path following the instruction at: http://bowtie-bio.sourceforge.net/bowtie2/manual.shtml#obtaining-bowtie-2')
        return False

#this is overkilling to run for many sequences,
#but for few is fine and effective.
def get_align_sequence(seq,bowtie2_index):
    cmd='''bowtie2 -x  %s -c -U %s |\
    grep -v '@' | awk '{OFS="\t"; bpstart=$4; split ($6,a,"[MIDNSHP]"); n=0;  bpend=bpstart;\
    for (i=1; i<=length(a); i++){\
      n+=1+length(a[i]); \
      if (substr($6,n,1)=="S"){\
          bpstart-=a[i];\
          if (bpend==$4)\
            bpend=bpstart;\
      } else if( (substr($6,n,1)!="I")  && (substr($6,n,1)!="H") )\
          bpend+=a[i];\
    }if (and($2, 16))print $3,bpstart,bpend,"-",$1,$10,$11;else print $3,bpstart,bpend,"+",$1,$10,$11;}' ''' %(bowtie2_index,seq)
    p = sb.Popen(cmd, shell=True,stdout=sb.PIPE)
    return p.communicate()[0]

#if a reference index is provided aligne the reads to it
#extract region
def get_region_from_fa(region,uncompressed_reference):
    p = sb.Popen("samtools faidx %s %s |   grep -v ^\> | tr -d '\n'" %(uncompressed_reference,region), shell=True,stdout=sb.PIPE)
    return p.communicate()[0]

def get_n_reads_compressed_fastq(compressed_fastq_filename):
     p = sb.Popen("zcat < %s | wc -l" % compressed_fastq_filename , shell=True,stdout=sb.PIPE)
     return float(p.communicate()[0])/4.0

#get a clean name that we can use for a filename
validFilenameChars = "+-_.() %s%s" % (string.ascii_letters, string.digits)

def clean_filename(filename):
    cleanedFilename = unicodedata.normalize('NFKD', unicode(filename)).encode('ASCII', 'ignore')
    return ''.join(c for c in cleanedFilename if c in validFilenameChars)

def get_avg_read_lenght_fastq(fastq_filename):
     cmd=('z' if fastq_filename.endswith('.gz') else '' ) +('cat < %s' % fastq_filename)+\
                  r''' | awk 'BN {n=0;s=0;} NR%4 == 2 {s+=length($0);n++;} END { printf("%d\n",s/n)}' '''
     p = sb.Popen(cmd, shell=True,stdout=sb.PIPE)
     return int(p.communicate()[0].strip())
    
    
def find_overlapping_genes(row):
    df_genes_overlapping=df_genes.ix[(df_genes.chrom==row.chr_id) &  
                                     (df_genes.txStart<=row.bpend) &  
                                     (row.bpstart<=df_genes.txEnd)]
    genes_overlapping=[]

    for idx_g,row_g in df_genes_overlapping.iterrows():
        genes_overlapping.append( '%s (%s)' % (row_g.name2,row_g['name']))

    row['gene_overlapping']=','.join(genes_overlapping)

    return row


#in bed file obtained from the sam file the end coordinate is not included
def extract_sequence_from_row(row):
    #bam to bed uses bam files so the coordinates are 0 based and not 1 based!
    return get_region_from_fa('%s:%d-%d' %(row.chr_id,row.bpstart,row.bpend),uncompressed_reference)

###EXCEPTIONS############################
class FlashException(Exception):
    pass

class TrimmomaticException(Exception):
    pass

class Bowtie2Exception(Exception):
    pass

class AmpliconsNotUniqueException(Exception):
    pass

class AmpliconsNamesNotUniqueException(Exception):
    pass

class NoReadsAlignedException(Exception):
    pass

class DonorSequenceException(Exception):
    pass

class AmpliconEqualDonorException(Exception):
    pass

class SgRNASequenceException(Exception):
    pass

class NTException(Exception):
    pass

class ExonSequenceException(Exception):
    pass


def main():

    print '  \n~~~CRISPRessoWGS~~~'
    print '-Analysis of CRISPR/Cas9 outcomes from WGS data-'
    print r'''
       )                                 )
      (           ____________          (
     __)__       |     __  __ |        __)__
  C\|     \      ||  |/ _ (_  |     C\|     \
    \     /      ||/\|\__)__) |       \     /
     \___/       |____________|        \___/
    '''

    print'\n[Luca Pinello 2015, send bugs, suggestions or *green coffee* to lucapinello AT gmail DOT com]\n\n',

    __version__ = re.search(
        '^__version__\s*=\s*"(.*)"',
        open(os.path.join(_ROOT,'CRISPRessoCORE.py')).read(),
        re.M
        ).group(1)
    print 'Version %s\n' % __version__


    parser = argparse.ArgumentParser(description='CRISPRessoPooled Parameters',formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-r1','--fastq_r1', type=str,  help='First fastq file', required=True,default='Fastq filename' )
    parser.add_argument('-r2','--fastq_r2', type=str,  help='Second fastq file for paired end reads',default='')
    parser.add_argument('-f','--amplicons_file', type=str,  help='Amplicons description file', default='')
    parser.add_argument('-x','--bowtie2_index', type=str, help='Basename of Bowtie2 index for the reference genome', default='')

    #tool specific optional
    parser.add_argument('--gene_annotations', type=str, help='Gene Annotation Table from UCSC Genome Browser Tables (http://genome.ucsc.edu/cgi-bin/hgTables?command=start), \
    please select as table "knowGene", as output format "all fields from selected table" and as file returned "gzip compressed"', default='')
    parser.add_argument('-p','--n_processes',help='Number of processes to use for the bowtie2 alignment',default=multiprocessing.cpu_count())
    parser.add_argument('--botwie2_options_string', type=str, help='Override options for the Bowtie2 alignment command',default=' -k 1 --end-to-end -N 0 --np 0 ')
    parser.add_argument('--min_perc_reads_to_use_region',  type=int, help='Minimum %% of reads that align to a region to perform the CRISPResso analysis', default=1.0)
    parser.add_argument('--min_reads_to_use_region',  type=float, help='Minimum number of reads that align to a region to perform the CRISPResso analysis', default=50)

    #general CRISPResso optional
    parser.add_argument('-q','--min_average_read_quality', type=int, help='Minimum average quality score (phred33) to keep a read', default=0)
    parser.add_argument('-s','--min_single_bp_quality', type=int, help='Minimum single bp score (phred33) to keep a read', default=0)
    parser.add_argument('--min_identity_score', type=float, help='Min identity score for the alignment', default=50.0)
    parser.add_argument('-n','--name',  help='Output name', default='')
    parser.add_argument('-o','--output_folder',  help='', default='')
    parser.add_argument('--trim_sequences',help='Enable the trimming of Illumina adapters with Trimmomatic',action='store_true')
    parser.add_argument('--trimmomatic_options_string', type=str, help='Override options for Trimmomatic',default=' ILLUMINACLIP:%s:0:90:10:0:true MINLEN:40' % get_data('NexteraPE-PE.fa'))
    parser.add_argument('--min_paired_end_reads_overlap',  type=int, help='Minimum required overlap length between two reads to provide a confident overlap. ', default=4)
    parser.add_argument('-w','--window_around_sgrna', type=int, help='Window(s) in bp around each sgRNA to quantify the indels. Any indels outside this window is excluded. A value of -1 disable this filter.', default=50)
    parser.add_argument('--exclude_bp_from_left', type=int, help='Exclude bp from the left side of the amplicon sequence for the quantification of the indels', default=0)
    parser.add_argument('--exclude_bp_from_right', type=int, help='Exclude bp from the right side of the amplicon sequence for the quantification of the indels', default=0)
    parser.add_argument('--hdr_perfect_alignment_threshold',  type=float, help='Sequence homology %% for an HDR occurrence', default=98.0)
    parser.add_argument('--needle_options_string',type=str,help='Override options for the Needle aligner',default='-gapopen=10 -gapextend=0.5  -awidth3=5000')
    parser.add_argument('--keep_intermediate',help='Keep all the  intermediate files',action='store_true')
    parser.add_argument('--dump',help='Dump numpy arrays and pandas dataframes to file for debugging purposes',action='store_true')
    parser.add_argument('--save_also_png',help='Save also .png images additionally to .pdf files',action='store_true')



    args = parser.parse_args()


    info('Checking dependencies...')

    if check_samtools() and check_bowtie2():
        print '\n\n All the required dependencies are present!'
    else:
        sys.exit(1)

    #check files
    check_file(args.fastq_r1)
    if args.fastq_r2:
        check_file(args.fastq_r2)

    if args.bowtie2_index:
        check_file(args.bowtie2_index+'.1.bt2')

    if args.amplicons_file:
        check_file(args.amplicons_file)

    if args.gene_annotations:
        check_file(args.gene_annotations)


    ####TRIMMING AND MERGING
    get_name_from_fasta=lambda  x: os.path.basename(x).replace('.fastq','').replace('.gz','')

    if not args.name:
             if args.fastq_r2!='':
                     database_id='%s_%s' % (get_name_from_fasta(args.fastq_r1),get_name_from_fasta(args.fastq_r2))
             else:
                     database_id='%s' % get_name_from_fasta(args.fastq_r1)

    else:
             database_id=args.name
            


    OUTPUT_DIRECTORY='CRISPRessoPOOLED_on_%s' % database_id

    if args.output_folder:
             OUTPUT_DIRECTORY=os.path.join(os.path.abspath(args.output_folder),OUTPUT_DIRECTORY)

    _jp=lambda filename: os.path.join(OUTPUT_DIRECTORY,filename) #handy function to put a file in the output directory

    try:
             info('Creating Folder %s' % OUTPUT_DIRECTORY)
             os.makedirs(OUTPUT_DIRECTORY)
             info('Done!')
    except:
             warn('Folder %s already exists.' % OUTPUT_DIRECTORY)

    log_filename=_jp('CRISPRessoPooled_RUNNING_LOG.txt')

    with open(log_filename,'w+') as outfile:
              outfile.write('[Command used]:\nCRISPRessoPooled %s\n\n[Execution log]:\n' % ' '.join(sys.argv))

    if args.fastq_r2=='': #single end reads

         #check if we need to trim
         if not args.trim_sequences:
             #create a symbolic link
             force_symlink(args.fastq_r1,_jp(os.path.basename(args.fastq_r1)))
             output_forward_filename=args.fastq_r1
         else:
             output_forward_filename=_jp('reads.trimmed.fq.gz')
             #Trimming with trimmomatic
             cmd='java -jar %s SE -phred33 %s  %s %s >>%s 2>&1'\
             % (get_data('trimmomatic-0.33.jar'),args.fastq_r1,
                output_forward_filename,
                args.trimmomatic_options_string.replace('NexteraPE-PE.fa','TruSeq3-SE.fa'),
                log_filename)
             #print cmd
             TRIMMOMATIC_STATUS=sb.call(cmd,shell=True)

             if TRIMMOMATIC_STATUS:
                     raise TrimmomaticException('TRIMMOMATIC failed to run, please check the log file.')


         processed_output_filename=output_forward_filename

    else:#paired end reads case

         if not args.trim_sequences:
             output_forward_paired_filename=args.fastq_r1
             output_reverse_paired_filename=args.fastq_r2
         else:
             info('Trimming sequences with Trimmomatic...')
             output_forward_paired_filename=_jp('output_forward_paired.fq.gz')
             output_forward_unpaired_filename=_jp('output_forward_unpaired.fq.gz')
             output_reverse_paired_filename=_jp('output_reverse_paired.fq.gz')
             output_reverse_unpaired_filename=_jp('output_reverse_unpaired.fq.gz')

             #Trimming with trimmomatic
             cmd='java -jar %s PE -phred33 %s  %s %s  %s  %s  %s %s >>%s 2>&1'\
             % (get_data('trimmomatic-0.33.jar'),
                     args.fastq_r1,args.fastq_r2,output_forward_paired_filename,
                     output_forward_unpaired_filename,output_reverse_paired_filename,
                     output_reverse_unpaired_filename,args.trimmomatic_options_string,log_filename)
             #print cmd
             TRIMMOMATIC_STATUS=sb.call(cmd,shell=True)
             if TRIMMOMATIC_STATUS:
                     raise TrimmomaticException('TRIMMOMATIC failed to run, please check the log file.')

             info('Done!')


         #Merging with Flash
         info('Merging paired sequences with Flash...')
         cmd='flash %s %s --min-overlap %d --max-overlap 80  -z -d %s >>%s 2>&1' %\
         (output_forward_paired_filename,
          output_reverse_paired_filename,
          args.min_paired_end_reads_overlap,
          OUTPUT_DIRECTORY,log_filename)

         FLASH_STATUS=sb.call(cmd,shell=True)
         if FLASH_STATUS:
             raise FlashException('Flash failed to run, please check the log file.')

         info('Done!')

         flash_hist_filename=_jp('out.hist')
         flash_histogram_filename=_jp('out.histogram')
         flash_not_combined_1_filename=_jp('out.notCombined_1.fastq.gz')
         flash_not_combined_2_filename=_jp('out.notCombined_2.fastq.gz')

         processed_output_filename=_jp('out.extendedFrags.fastq.gz')




    if args.amplicons_file and not args.bowtie2_index:
        RUNNING_MODE='ONLY_AMPLICONS'
        info('Only Amplicon description file was provided. The analysis will be perfomed using only the provided amplicons sequences.')

    elif args.bowtie2_index and not args.amplicons_file:
        RUNNING_MODE='ONLY_GENOME'
        info('Only bowtie2 reference genome index file provided. The analysis will be perfomed using only genomic regions where enough reads align.')
    elif args.bowtie2_index and args.amplicons_file:
        RUNNING_MODE='AMPLICONS_AND_GENOME'
        info('Amplicon description file and bowtie2 reference genome index files provided. The analysis will be perfomed using the reads that are aligned ony to the amplicons provided and not to other genomic regions.')
    else:
        error('Please provide the amplicons description file (-t or --amplicons_file option) or the bowtie2 reference genome index file (-x or --bowtie2_index option) or both.')
        sys.exit(1)
        
    #load gene annotation
    if args.gene_annotations:
        print 'Loading gene coordinates from annotation file: %s...' % args.gene_annotations
        try:
            df_genes=pd.read_table(args.gene_annotations,compression='gzip')
            df_genes.head()
        except:
            print 'Failed to load the gene annotations file.'
        
        
        
        
        
        


    if RUNNING_MODE=='ONLY_AMPLICONS' or  RUNNING_MODE=='AMPLICONS_AND_GENOME':

        #load and validate template file
        df_template=pd.read_csv(args.amplicons_file,names=[
                'Name','Amplicon_Sequence','sgRNA',
                'Expected_HDR','Coding_sequence'],comment='#',sep='\t')


        #remove empty amplicons/lines
        df_template.dropna(subset=['Amplicon_Sequence'],inplace=True)
        df_template.dropna(subset=['Name'],inplace=True)

        df_template.Amplicon_Sequence=df_template.Amplicon_Sequence.apply(capitalize_sequence)
        df_template.Expected_HDR=df_template.Expected_HDR.apply(capitalize_sequence)
        df_template.sgRNA=df_template.sgRNA.apply(capitalize_sequence)
        df_template.Coding_sequence=df_template.Coding_sequence.apply(capitalize_sequence)

        if not len(df_template.Amplicon_Sequence.unique())==df_template.shape[0]:
            raise Exception('The amplicons should be all distinct!')

        if not len(df_template.Name.unique())==df_template.shape[0]:
            raise Exception('The amplicon names should be all distinct!')

        df_template=df_template.set_index('Name')

        for idx,row in df_template.iterrows():

            wrong_nt=find_wrong_nt(row.Amplicon_Sequence)
            if wrong_nt:
                 raise NTException('The amplicon sequence %s contains wrong characters:%s' % (row.Name,' '.join(wrong_nt)))

            if not pd.isnull(row.sgRNA):
                wrong_nt=find_wrong_nt(row.sgRNA.strip().upper())
                if wrong_nt:
                    raise NTException('The sgRNA sequence %s contains wrong characters:%s'  % ' '.join(wrong_nt))

                cut_points=[m.start() +len(row.sgRNA)-3 for m in re.finditer(row.sgRNA, row.Amplicon_Sequence)]+[m.start() +2 for m in re.finditer(reverse_complement(row.sgRNA), row.Amplicon_Sequence)]

                if not cut_points:
                    raise SgRNASequenceException('The guide sequence/s provided is(are) not present in the amplicon sequence! \n\nPlease check your input!')


    if RUNNING_MODE=='ONLY_AMPLICONS':
        #create a fasta file with all the amplicons
        amplicon_fa_filename=_jp('AMPLICONS.fa')
        fastq_gz_amplicon_filenames=[]
        with open(amplicon_fa_filename,'w+') as outfile:
            for idx,row in df_template.iterrows():
                if row['Amplicon_Sequence']:
                    outfile.write('>%s\n%s\n' %(clean_filename('AMPL_'+idx),row['Amplicon_Sequence']))
    
                    #create place-holder fastq files
                    fastq_gz_amplicon_filenames.append(_jp('%s.fastq.gz' % clean_filename('AMPL_'+idx)))
                    open(fastq_gz_amplicon_filenames[-1], 'w+').close()
    
        df_template['Demultiplexed_fastq.gz_filename']=fastq_gz_amplicon_filenames
        #create a custom index file with all the amplicons
        custom_index_filename=_jp('CUSTOM_BOWTIE2_INDEX')
        sb.call('bowtie2-build %s %s' %(amplicon_fa_filename,custom_index_filename), shell=True)
    
    
        ###LOG FILE####
    
        #align the file to the amplicons (MODE 1)
        bam_filename_amplicons= _jp('CRISPResso_AMPLICONS_ALIGNED.bam')
        aligner_command= 'bowtie2 -x %s -p %s -k 1 --end-to-end -N 0 --np 0 -U %s | samtools view -bS - > %s' %(custom_index_filename,args.n_processes,args.fastq_r1,bam_filename_amplicons)

        sb.call(aligner_command,shell=True)
    
    
        s1=r"samtools view -F 4 %s | grep -v ^'@'" % bam_filename_amplicons
        s2=r'''|awk '{ gzip_filename=sprintf("gzip >> OUTPUTPATH%s.fastq.gz",$3);\
        print "@"$1"\n"$10"\n+\n"$11  | gzip_filename;}' '''
        
        cmd=s1+s2.replace('OUTPUTPATH',_jp(''))
        
        print cmd
        sb.call(cmd,shell=True)
    
        n_reads_aligned_amplicons=[]
        for idx,row in df_template.iterrows():
            n_reads_aligned_amplicons.append(get_n_reads_compressed_fastq(row['Demultiplexed_fastq.gz_filename']))
            crispresso_cmd='CRISPResso -r1 %s -a %s -o %s' % (row['Demultiplexed_fastq.gz_filename'],row['Amplicon_Sequence'],OUTPUT_DIRECTORY)
    
            if n_reads_aligned_amplicons[-1]:
                if row['sgRNA'] and not pd.isnull(row['sgRNA']):
                    crispresso_cmd+=' -g %s' % row['sgRNA'] 
    
                if row['Expected_HDR'] and not pd.isnull(row['Expected_HDR']):
                    crispresso_cmd+=' -e %s' % row['Expected_HDR'] 
    
                if row['Coding_sequence'] and not pd.isnull(row['Coding_sequence']):
                    crispresso_cmd+=' -c %s' % row['Coding_sequence'] 
    
                print crispresso_cmd
                sb.call(crispresso_cmd,shell=True)
            else:
                print '\nWARNING: Skipping Amplicon %s since no reads are aligning to it\n'% idx
    
        df_template['n_reads']=n_reads_aligned_amplicons
        df_template.to_csv(_jp('REPORT_READS_ALIGNED_TO_AMPLICONS.txt'),sep='\t')
    

    if RUNNING_MODE=='ONLY_GENOME':

        ###HERE we recreate the uncompressed genome file if not available###

        #check you have all the files for the genome and create a fa idx for samtools
        uncompressed_reference=_jp(GENOME_LOCAL_FOLDER,'UNCOMPRESSED_REFERENCE_FROM_'+args.bowtie2_index.replace('/','_')+'.fa')

        if not os.path.exists(GENOME_LOCAL_FOLDER):
            os.mkdir(GENOME_LOCAL_FOLDER)

        if os.path.exists(uncompressed_reference):
            info('The uncompressed reference fasta file for %s is already present! Skipping generation.' % args.bowtie2_index)
        else:
            info('Extracting uncompressed reference from the provided bowtie2 index...\nPlease be patient!')

            cmd_to_uncompress='bowtie2-inspect %s > %s' % (args.bowtie2_index,uncompressed_reference)
            print cmd_to_uncompress
            sb.call(cmd_to_uncompress,shell=True)

            info('Indexing fasta file with samtools...')
            #!samtools faidx {uncompressed_reference}
            sb.call('samtools faidx %s' % uncompressed_reference,shell=True)


if __name__ == '__main__':
    main()
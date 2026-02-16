       IDENTIFICATION DIVISION.
       PROGRAM-ID. DEMOCOB.
       DATA DIVISION.
       FILE SECTION.
       FD  FOUT.
       01  OUT-REC.
           05 REC-TYPE         PIC X(3).
           05 INVOICE-NO       PIC 9(10).
           05 AMOUNT-TTC       PIC S9(7)V99 COMP-3.
           05 LABEL-TEXT       PIC X(20).
       PROCEDURE DIVISION.
           OPEN OUTPUT FOUT.
           WRITE OUT-REC.
           STOP RUN.
